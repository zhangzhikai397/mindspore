# Copyright 2020 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Record the summary event."""
import os
import re
import threading

from mindspore import log as logger

from ..._c_expression import Tensor
from ..._checkparam import _check_str_by_regular
from .._utils import _make_directory
from ._event_writer import EventWriter
from ._summary_adapter import get_event_file_name, package_graph_event, package_init_event

# for the moment, this lock is for caution's sake,
# there are actually no any concurrencies happening.
_summary_lock = threading.Lock()
# cache the summary data
_summary_tensor_cache = {}


def _cache_summary_tensor_data(summary):
    """
    Get the time of ms.

    Args:
         summary (list): [{"name": tag_name, "data": tensor}, {"name": tag_name, "data": tensor},...].
    """
    with _summary_lock:
        if "SummaryRecord" in _summary_tensor_cache:
            _summary_tensor_cache["SummaryRecord"].extend(summary)
        else:
            _summary_tensor_cache["SummaryRecord"] = summary
        return True


def _get_summary_tensor_data():
    if 'SummaryRecord' not in _summary_tensor_cache:
        return None
    with _summary_lock:
        return _summary_tensor_cache.pop('SummaryRecord')


class SummaryRecord:
    """
    SummaryRecord is used to record the summary value.

    Note:
        The API will create an event file in a given directory and add summaries and events to it.
        It writes the event log to a file by executing the record method. In addition,
        if the SummaryRecord object is created and the summary operator is used in the network,
        even if the record method is not called, the event in the cache will be written to the
        file at the end of execution or when the summary is closed.

    Args:
        log_dir (str): The log_dir is a directory location to save the summary.
        queue_max_size (int): The capacity of event queue.(reserved). Default: 0.
        flush_time (int): Frequency to flush the summaries to disk, the unit is second. Default: 120.
        file_prefix (str): The prefix of file. Default: "events".
        file_suffix (str): The suffix of file. Default: "_MS".
        network (Cell): Obtain a pipeline through network for saving graph summary. Default: None.

    Raises:
        TypeError: If `queue_max_size` and `flush_time` is not int, or `file_prefix` and `file_suffix` is not str.
        RuntimeError: If the log_dir can not be resolved to a canonicalized absolute pathname.

    Examples:
        >>> summary_record = SummaryRecord(log_dir="/opt/log", queue_max_size=50, flush_time=6,
        >>>                                file_prefix="xxx_", file_suffix="_yyy")
    """

    def __init__(self,
                 log_dir,
                 queue_max_size=0,
                 flush_time=120,
                 file_prefix="events",
                 file_suffix="_MS",
                 network=None):

        _check_str_by_regular(file_prefix)
        _check_str_by_regular(file_suffix)
        self.log_path = _make_directory(log_dir)

        if not isinstance(queue_max_size, int) or not isinstance(flush_time, int):
            raise TypeError("`queue_max_size` and `flush_time` should be int")
        if not isinstance(file_prefix, str) or not isinstance(file_suffix, str):
            raise TypeError("`file_prefix` and `file_suffix`  should be str.")

        self.queue_max_size = queue_max_size
        if queue_max_size < 0:
            # 0 is not limit
            logger.warning("The queue_max_size(%r) set error, will use the default value: 0", queue_max_size)
            self.queue_max_size = 0

        self.flush_time = flush_time
        if flush_time <= 0:
            logger.warning("The flush_time(%r) set error, will use the default value: 120", flush_time)
            self.flush_time = 120

        self.prefix = file_prefix
        self.suffix = file_suffix
        self.network = network
        self.has_graph = False
        self._closed = False

        # create the summary writer file
        self.event_file_name = get_event_file_name(self.prefix, self.suffix)
        try:
            self.full_file_name = os.path.join(self.log_path, self.event_file_name)
        except Exception as ex:
            raise RuntimeError(ex)
        self.event_writer = EventWriter(self.full_file_name, self.flush_time)
        self.event_writer.write(package_init_event().SerializeToString())

    def record(self, step, train_network=None):
        """
        Record the summary.

        Args:
            step (int): Represents training step number.
            train_network (Cell): The network that called the callback.

        Examples:
            >>> summary_record = SummaryRecord(log_dir="/opt/log", queue_max_size=50, flush_time=6,
            >>>                                file_prefix="xxx_", file_suffix="_yyy")
            >>> summary_record.record(step=2)

        Returns:
            bool, whether the record process is successful or not.
        """
        logger.info("SummaryRecord step is %r.", step)
        if self._closed:
            logger.error("The record writer is closed.")
            return False
        if not isinstance(step, int) or isinstance(step, bool):
            raise ValueError("`step` should be int")
        # Set the current summary of train step

        if self.network is not None and not self.has_graph:
            graph_proto = self.network.get_func_graph_proto()
            if graph_proto is None and train_network is not None:
                graph_proto = train_network.get_func_graph_proto()
            if graph_proto is None:
                logger.error("Failed to get proto for graph")
            else:
                self.event_writer.write(package_graph_event(graph_proto).SerializeToString())
                self.has_graph = True
                if _summary_tensor_cache.get('SummaryRecord') is None:
                    return True

        data = _get_summary_tensor_data()
        if data is None:
            logger.error("The step(%r) does not have record data.", step)
            return False
        if self.queue_max_size > 0 and len(data) > self.queue_max_size:
            logger.error("The size of data record is %r, which is greater than queue_max_size %r.", len(data),
                         self.queue_max_size)

        # process the data
        result = self._data_convert(data)
        if not result:
            logger.error("The step(%r) summary data is invalid.", step)
            return False
        self.event_writer.write((result, step))
        logger.debug("Send the summary data to scheduler for saving, step = %d", step)
        return True

    @property
    def log_dir(self):
        """
        Get the full path of the log file.

        Examples:
            >>> summary_record = SummaryRecord(log_dir="/opt/log", queue_max_size=50, flush_time=6,
            >>>                                file_prefix="xxx_", file_suffix="_yyy")
            >>> print(summary_record.log_dir)

        Returns:
            String, the full path of log file.
        """
        return self.full_file_name

    def flush(self):
        """
        Flush the event file to disk.

        Call it to make sure that all pending events have been written to disk.

        Examples:
            >>> summary_record = SummaryRecord(log_dir="/opt/log", queue_max_size=50, flush_time=6,
            >>>                                file_prefix="xxx_", file_suffix="_yyy")
            >>> summary_record.flush()
        """
        if self._closed:
            logger.error("The record writer is closed and can not flush.")
        else:
            self.event_writer.flush()

    def close(self):
        """
        Flush all events and close summary records.

        Examples:
            >>> summary_record = SummaryRecord(log_dir="/opt/log", queue_max_size=50, flush_time=6,
            >>>                                file_prefix="xxx_", file_suffix="_yyy")
            >>> summary_record.close()
        """
        if not self._closed:
            # event writer flush and close
            self.event_writer.close()
            self._closed = True

    def _data_convert(self, summary):
        """Convert the data."""
        if summary is None:
            logger.warning("The step does not have record data.")
            return None

        # convert the summary to numpy
        result = []
        for v_dict in summary:
            name = v_dict["name"]
            data = v_dict["data"]
            # confirm the data is valid
            summary_tag, summary_type = SummaryRecord._parse_from(name)
            if summary_tag is None:
                logger.error("The data type is invalid, name = %r, tensor = %r", name, data)
                return None
            if isinstance(data, Tensor):
                result.append({'name': summary_tag, 'data': data.asnumpy(), '_type': summary_type})
            else:
                logger.error("The data type is invalid, name = %r, tensor = %r", name, data)
                return None

        return result

    @staticmethod
    def _parse_from(name: str = None):
        """
        Parse the tag and type from name.

        Args:
            name (str): Format: TAG[:TYPE].

        Returns:
            Tuple, (summary_tag, summary_type).
        """
        if name is None:
            logger.error("The name is None")
            return None, None
        match = re.match(r'(.+)\[:(.+)\]', name)
        if match:
            return match.groups()
        logger.error("The name(%r) format is invalid, expected 'TAG[:TYPE]'.", name)
        return None, None
