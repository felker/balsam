import subprocess
import sys
import shlex
import os
from datetime import datetime
from collections import namedtuple

from django.conf import settings
from balsam.service.schedulers.exceptions import * 
from balsam.service.schedulers import Scheduler

import logging
logger = logging.getLogger(__name__)

def new_scheduler():
    return CobaltScheduler()

class CobaltScheduler(Scheduler.Scheduler):
    SCHEDULER_VARIABLES = {
        'current_scheduler_id' : 'COBALT_JOBID',
        'num_workers'  : 'COBALT_PARTSIZE',
        'workers_str'  : 'COBALT_PARTNAME',
        'workers_file' : 'COBALT_NODEFILE',
    }
    JOBSTATUS_VARIABLES = {
        'id' : 'JobID',
        'time_remaining' : 'TimeRemaining',
        'state' : 'State',
    }
    GENERIC_NAME_MAP = {v:k for k,v in JOBSTATUS_VARIABLES.items()}
    QSTAT_EXE = settings.SCHEDULER_STATUS_EXE

    def _make_submit_cmd(self, script_path, qlaunch):
        exe = settings.SCHEDULER_SUBMIT_EXE # qsub
        cwd = settings.SERVICE_PATH
        return f"{exe} -O qlaunch{qlaunch.pk} --cwd {cwd} script_path"

    def _parse_submit_output(self, submit_output):
        try: scheduler_id = int(output)
        except ValueError: scheduler_id = int(output.split()[-1])
        return dict(scheduler_id=scheduler_id)

    def get_status(self, scheduler_id, jobstatus_vars=None):
        if jobstatus_vars is None: 
            jobstatus_vars = self.JOBSTATUS_VARIABLES.values()
        else:
            jobstatus_vars = [self.JOBSTATUS_VARIABLES[a] for a in jobstatus_vars]

        logger.debug(f"Cobalt ID {scheduler_id} get_status:")
        info = self._qstat(scheduler_id, jobstatus_vars)
        info = {self.GENERIC_NAME_MAP[k] : v for k,v in info.items()}

        time_attrs_seconds = {k+"_sec" : datetime.strptime(v, '%H:%M:%S')
                              for k,v in info.items() if 'time' in k}
        for k,time in time_attrs_seconds.items():
            time_attrs_seconds[k] = time.hour*3600 + time.minute*60 + time.second
        info.update(time_attrs_seconds)
        logger.debug(str(info))
        return info

    def _qstat(self, scheduler_id, attrs):
        qstat_cmd = f"{self.QSTAT_EXE} {scheduler_id}"
        env = {'QSTAT_HEADER': ':'.join(attrs)}

        try:
            p = subprocess.Popen(shlex.split(qstat_cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env)
        except OSError:
            raise JobStatusFailed(f"could not execute {qstat_cmd}")

        stdout, _ = p.communicate()
        stdout = stdout.decode('utf-8')
        if p.returncode != 0:
            logger.exception('return code for qstat is non-zero:\n'+stdout)
            raise NoQStatInformation("qstat nonzero return code: this might signal job is done")
        try:
            logger.debug('parsing qstat ouput: \n' + stdout)
            qstat_fields = stdout.split('\n')[2].split()
            qstat_info = {attr : qstat_fields[i] for (i,attr) in
                               enumerate(attrs)}
        except:
            raise NoQStatInformation
        else:
            return qstat_info
