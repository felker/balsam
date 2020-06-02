from collections import defaultdict, Counter
from .scheduler import SubprocessSchedulerInterface, JobStatus, BackfillWindow
import os
import logging

logger = logging.getLogger(__name__)


def parse_cobalt_time_minutes(t_str):
    try:
        H, M, S = map(int, t_str.split(":"))
    except ValueError:
        return 0
    else:
        return H * 60 + M + round(S / 60)


class CobaltScheduler(SubprocessSchedulerInterface):
    status_exe = "qstat"
    submit_exe = "qsub"
    delete_exe = "qdel"
    backfill_exe = "nodelist"

    # maps scheduler states to Balsam states
    job_states = {
        "queued": "queued",
        "starting": "starting",
        "running": "running",
        "exiting": "exiting",
        "user_hold": "user_hold",
        "dep_hold": "dep_hold",
        "dep_fail": "dep_fail",
        "admin_hold": "admin_hold",
        "finished": "finished",
        "failed": "failed",
    }

    @staticmethod
    def _job_state_map(scheduler_state):
        return CobaltScheduler.job_states.get(scheduler_state, "unknown")

    # maps Balsam status fields to the scheduler fields
    # should be a comprehensive list of scheduler status fields
    status_fields = {
        "id": "JobID",
        "state": "State",
        "wall_time_min": "WallTime",
        "queue": "Queue",
        "num_nodes": "Nodes",
        "project": "Project",
        "time_remaining_min": "TimeRemaining",
    }

    # when reading these fields from the scheduler apply
    # these maps to the string extracted from the output
    @staticmethod
    def _status_field_map(balsam_field):
        status_field_map = {
            "id": lambda id: int(id),
            "num_nodes": lambda n: int(n),
            "time_remaining_min": parse_cobalt_time_minutes,
            "wall_time_min": parse_cobalt_time_minutes,
            "state": CobaltScheduler._job_state_map,
        }
        return status_field_map.get(balsam_field, lambda x: x)

    # maps node list states to Balsam node states
    node_states = {
        "busy": "busy",
        "idle": "idle",
        "cleanup-pending": "busy",
        "down": "busy",
        "allocated": "busy",
    }

    @staticmethod
    def _node_state_map(nodelist_state):
        try:
            return CobaltScheduler.node_states[nodelist_state]
        except KeyError:
            logger.warning("node state %s is not recognized", nodelist_state)
            return "unknown"

    # maps the Balsam status fields to the node list fields
    # should be a comprehensive list of node list fields
    nodelist_fields = {
        "id": "Node_id",
        "name": "Name",
        "queues": "Queues",
        "state": "Status",
        "mem": "MCDRAM",
        "numa": "NUMA",
        "backfill_time_min": "Backfill",
    }

    # when reading these fields from the scheduler apply
    # these maps to the string extracted from the output
    @staticmethod
    def _nodelist_field_map(balsam_field):
        nodelist_field_map = {
            "id": lambda id: int(id),
            "state": CobaltScheduler._node_state_map,
            "queues": lambda x: x.split(":"),
            "backfill_time_min": lambda x: parse_cobalt_time_minutes(x),
        }
        return nodelist_field_map.get(balsam_field, lambda x: x)

    def _get_envs(self):
        env = {}
        fields = self.status_fields.values()
        env["QSTAT_HEADER"] = ":".join(fields)
        return env

    def _render_submit_args(self, script_path, project, queue, num_nodes, time_minutes):
        args = [
            self.submit_exe,
            # '--cwd', site.job_path,
            "-O",
            os.path.basename(os.path.splitext(script_path)[0]),
            "-A",
            project,
            "-q",
            queue,
            "-n",
            str(int(num_nodes)),
            "-t",
            str(int(time_minutes)),
            script_path,
        ]
        return args

    def _render_status_args(self, project=None, user=None, queue=None):
        args = [self.status_exe]
        if user is not None:
            args += ["-u", user]
        if project is not None:
            args += ["-A", project]
        if queue is not None:
            args += ["-q", queue]
        return args

    def _render_delete_args(self, job_id):
        return [self.delete_exe, str(job_id)]

    def _render_backfill_args(self):
        return [self.backfill_exe]

    def _parse_submit_output(self, submit_output):
        try:
            scheduler_id = int(submit_output)
        except ValueError:
            scheduler_id = int(submit_output.split("\n")[2])
        return scheduler_id

    def _parse_status_output(self, raw_output):
        # TODO: this can be much more efficient with a compiled regex findall()
        status_dict = {}
        job_lines = raw_output.split("\n")[2:]
        for line in job_lines:
            try:
                job_stat = self._parse_status_line(line)
            except (ValueError, TypeError):
                logger.debug(f"Cannot parse job status: {line}")
                continue
            else:
                status_dict[job_stat.id] = job_stat
        return status_dict

    def _parse_status_line(self, line):
        fields = line.split()
        actual = len(fields)
        expected = len(self.status_fields)
        if actual != expected:
            raise ValueError(
                f"Line has {actual} columns: expected {expected}:\n{fields}"
            )

        status = {}
        for name, value in zip(self.status_fields, fields):
            func = self._status_field_map(name)
            status[name] = func(value)
        return JobStatus(**status)

    def _parse_backfill_output(self, stdout):
        raw_lines = stdout.split("\n")
        nodelist = []
        node_lines = raw_lines[2:]
        for line in node_lines:
            try:
                line_dict = self._parse_nodelist_line(line)
            except (ValueError, TypeError):
                logger.debug(f"Cannot parse nodelist line: {line}")
            else:
                if line_dict["backfill_time_min"] > 0 and line_dict["state"] == "idle":
                    nodelist.append(line_dict)

        windows = self._nodelist_to_backfill(nodelist)
        return windows

    def _parse_nodelist_line(self, line):
        fields = line.split()
        actual = len(fields)
        expected = len(self.nodelist_fields)

        if actual != expected:
            raise ValueError(
                f"Line has {actual} columns: expected {expected}:\n{fields}"
            )

        status = {}
        for name, value in zip(self.nodelist_fields, fields):
            func = CobaltScheduler._nodelist_field_map(name)
            status[name] = func(value)
        return status

    def _nodelist_to_backfill(self, nodelist):
        queue_bf_times = defaultdict(list)
        windows = defaultdict(list)

        for entry in nodelist:
            bf_time = entry["backfill_time_min"]
            queues = entry["queues"]
            for queue in queues:
                queue_bf_times[queue].append(bf_time)

        for queue, bf_times in queue_bf_times.items():
            bf_counter = Counter(bf_times)  # mapping {bf_time: num_nodes}
            bf_counter = sorted(bf_counter.items(), reverse=True)  # longer times first
            queue_bf_times[queue] = bf_counter

        for queue, bf_counter in queue_bf_times.items():
            running_total = 0
            for bf_time, num_nodes in bf_counter:
                running_total += num_nodes
                windows[queue].append(
                    BackfillWindow(num_nodes=running_total, backfill_time_min=bf_time)
                )
        return windows