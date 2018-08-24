'''BalsamJob Transitions

The user selects ``NUM_TRANSITION_THREADS`` processes to run
alongside the main Launcher process.  These are created with ``multiprocessing.Process``
and communicate with the Launcher through a ``multiprocessing.Queue`` (or a
PriorityQueue via a ``multiprocessing.managers.SyncManager``)

The transition processes pull jobs from the queue, execute the necessary
transition function, and signal completion by putting a status message back on a
status queue. These transition processes explicitly ignore SIGINT and SIGTERM
interrupts, so that they can finish the current transition and exit gracefully,
under control of the main Launcher process.
'''
from collections import defaultdict
import glob
import multiprocessing
import os
from io import StringIO
from traceback import print_exc
import random
import signal
import shutil
import subprocess
import time
import tempfile

from django import db

from balsam.common import transfer 
from balsam.launcher.exceptions import *
try:
    from balsam.service.models import BalsamJob
except:
    from balsam.launcher import dag
    BalsamJob = dag.BalsamJob

from balsam.service.models import PROCESSABLE_STATES
from balsam.launcher.util import get_tail

import logging
logger = logging.getLogger('balsam.launcher.transitions')

PREPROCESS_TIMEOUT_SECONDS = 300
POSTPROCESS_TIMEOUT_SECONDS = 300
EXIT_FLAG = False

def handler(signum, stack):
    global EXIT_FLAG
    EXIT_FLAG = True

class TransitionProcessPool:
    '''Launch and terminate the transition processes'''
    def __init__(self, num_threads, wf_name):

        self.procs = [
            multiprocessing.Process(
                target=main,
                args=(num_threads, wf_name)
            )
            for i in range(num_threads)
        ]
        logger.debug(f"Starting {len(self.procs)} transition processes")
        db.connections.close_all()
        for proc in self.procs:
            proc.daemon = True
            proc.start()

    def terminate(self):
        '''Terminate workers via signal and process join'''
        logger.debug("Sending sigterm and waiting on transition processes")
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs: 
            proc.join()
        logger.debug("All Transition processes joined: done.")


def update_states_from_cache(job_cache):
    # Update states of fast-forwarded jobs
    update_jobs = defaultdict(list)
    for job in job_cache:
        if job.state != job.__old_state:
            update_jobs[job.state].append(job.pk)
            job.__old_state = job.state
    if not update_jobs: return
    with db.transaction.atomic():
        for newstate, joblist in update_jobs.items():
            BalsamJob.batch_update_state(joblist, newstate)

def refresh_cache(job_cache, num_threads):
    manager = BalsamJob.source
    num_transitionable = BalsamJob.objects.filter(state__in=PROCESSABLE_STATES).count()
    target_count = round(num_transitionable / num_threads + 0.5)
    num_to_acquire = max(0, target_count-len(job_cache))
    acquired = manager.acquire_transitionable(num_to_acquire)
    if acquired:
        logger.debug(f'Acquired {len(acquired)} new jobs: {[j.cute_id for j in acquired]}')
    job_cache.extend(acquired)
    for job in job_cache: job.__old_state = job.state

def release_jobs(job_cache):
    manager = BalsamJob.source
    release_jobs = [j.pk for j in job_cache if j.state not in PROCESSABLE_STATES]
    manager.release(release_jobs)
    return [j for j in job_cache if j.pk not in release_jobs]

def main(num_threads, wf_name):
    global EXIT_FLAG
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    
    manager = BalsamJob.source
    manager.workflow = wf_name
    time.sleep(random.random())
    manager.start_tick()

    try:
        _main(num_threads)
    except:
        buf = StringIO()
        print_exc(file=buf)
        logger.critical(f"Uncaught exception:\n%s", buf.getvalue())
    finally:
        manager.release_all_owned()
        logger.debug('Transition process finished: released all jobs')

def _main(num_threads):
    global EXIT_FLAG
    manager = BalsamJob.source
    job_cache = []
    last_refresh = 0
    refresh_period = 5

    while not EXIT_FLAG:
        # Update in-memory cache of locked BalsamJobs
        elapsed = time.time() - last_refresh
        if not job_cache or elapsed > refresh_period:
            refresh_cache(job_cache, num_threads)
            last_refresh = time.time()
            if elapsed < 1: time.sleep(1 - elapsed)

        # Fast-forward transitions & release locks
        fast_forward(job_cache)
        job_cache = release_jobs(job_cache)

        # Run transitions (one pass over all jobs)
        for job in job_cache:
            transition_function = TRANSITIONS[job.state]
            try:
                transition_function(job)
            except BalsamTransitionError as e:
                job.state = 'FAILED' 
                buf = StringIO()
                print_exc(file=buf)
                logger.exception(f"{job.cute_id} BalsamTransitionError:\n%s\n", buf.getvalue())
                logger.exception(f"Marking {job.cute_id} as FAILED")
            if EXIT_FLAG:
                break
        # Update states in bulk
        update_states_from_cache(job_cache)
        job_cache = release_jobs(job_cache)
    logger.info('EXIT_FLAG: exiting main loop')


def check_parents(job):
    '''Check job's dependencies, update to READY if satisfied'''
    num_parents = len(job.get_parents_by_id())

    if num_parents == 0 or not job.wait_for_parents:
        ready = True
    else:
        parents = job.get_parents()
        ready = num_parents == parents.filter(state='JOB_FINISHED').count()

    if ready:
        job.state = 'READY'
        logger.debug(f'{job.cute_id} ready')
    elif job.state != 'AWAITING_PARENTS':
        job.state = 'AWAITING_PARENTS'
        logger.debug(f'{job.cute_id} waiting for {num_parents} parents')

def fast_forward(job_cache):
    '''Make several passes over the job list; advancing states in order'''
    # Check parents
    check_jobs = (j for j in job_cache if j.state in 'CREATED AWAITING_PARENTS'.split())
    for job in check_jobs: check_parents(job)

    # Skip stage-in
    stagein_jobs = (j for j in job_cache if j.state == 'READY')
    for job in stagein_jobs:
        workdir = job.working_directory
        if not os.path.exists(workdir):
            os.makedirs(workdir)
            logger.debug(f"{job.cute_id} working directory {workdir}")
        hasParents = bool(job.get_parents_by_id())
        hasInput = bool(job.input_files)
        hasRemote = bool(job.stage_in_url)
        if not hasRemote and not (hasParents and hasInput): job.state = 'STAGED_IN'

    # Skip preprocess
    preprocess_jobs = (j for j in job_cache if j.state == 'STAGED_IN')
    for job in preprocess_jobs:
        if not job.preprocess: job.state = 'PREPROCESSED'

    # RUN_DONE: skip postprocess
    done_jobs = (j for j in job_cache if j.state=='RUN_DONE' and not j.postprocess)
    for job in done_jobs: job.state = 'POSTPROCESSED'

    # Timeout: retry
    retry_jobs = (j for j in job_cache if j.state=='RUN_TIMEOUT' and j.auto_timeout_retry and not j.post_timeout_handler)
    for job in retry_jobs: job.state = 'RESTART_READY'
    
    # Timeout: fail
    timefail_jobs = (j for j in job_cache if j.state=='RUN_TIMEOUT'
                     and not j.auto_timeout_retry
                     and not (j.postprocess and j.post_timeout_handler)
                    )
    for job in timefail_jobs: job.state = 'FAILED'

    # Error: fail 
    errfail_jobs = (j for j in job_cache if j.state=='RUN_ERROR'
                    and not (j.post_error_handler and j.postprocess)
                   )
    for job in errfail_jobs: job.state = 'FAILED'

    # skip stageout (finished)
    stageout_jobs = (j for j in job_cache if j.state=='POSTPROCESSED'
                     and not (j.stage_out_url and j.stage_out_files)
                    )
    for job in stageout_jobs: job.state = 'JOB_FINISHED'
    update_states_from_cache(job_cache)


def stage_in(job):
    logger.debug(f'{job.cute_id} in stage_in')

    work_dir = job.working_directory
    if not os.path.exists(work_dir):
        os.makedirs(workdir)
        logger.debug(f"{job.cute_id} working directory {work_dir}")

    # stage in all remote urls
    # TODO: stage_in remote transfer should allow a list of files and folders,
    # rather than copying just one entire folder
    url_in = job.stage_in_url
    if url_in:
        logger.info(f"{job.cute_id} transfer in from {url_in}")
        try:
            transfer.stage_in(f"{url_in}",  f"{work_dir}")
        except Exception as e:
            message = 'Exception received during stage_in: ' + str(e)
            raise BalsamTransitionError(message) from e

    # create unique symlinks to "input_files" patterns from parents
    # TODO: handle data flow from remote sites transparently
    matches = []
    parents = job.get_parents()
    input_patterns = job.input_files.split()
    logger.debug(f"{job.cute_id} searching parent workdirs for {input_patterns}")
    for parent in parents:
        parent_dir = parent.working_directory
        for pattern in input_patterns:
            path = os.path.join(parent_dir, pattern)
            matches.extend((parent.pk,match) 
                           for match in glob.glob(path))

    for parent_pk, inp_file in matches:
        basename = os.path.basename(inp_file)
        new_path = os.path.join(work_dir, basename)
        
        if os.path.exists(new_path): new_path += f"_{str(parent_pk)[:8]}"
        # pointing to src, named dst
        logger.info(f"{job.cute_id}   {new_path}  -->  {inp_file}")
        try:
            os.symlink(src=inp_file, dst=new_path)
        except Exception as e:
            raise BalsamTransitionError(
                f"Exception received during symlink: {e}") from e

    job.state = 'STAGED_IN'
    logger.info(f"{job.cute_id} stage_in done")


def stage_out(job):
    '''copy from the local working_directory to the output_url '''
    logger.debug(f'{job.cute_id} in stage_out')

    url_out = job.stage_out_url
    if not url_out:
        job.state = 'JOB_FINISHED'
        logger.info(f'{job.cute_id} no stage_out_url: done')
        return

    stage_out_patterns = job.stage_out_files.split()
    logger.debug(f"{job.cute_id} stage out files match: {stage_out_patterns}")
    work_dir = job.working_directory
    matches = []
    for pattern in stage_out_patterns:
        path = os.path.join(work_dir, pattern)
        matches.extend(glob.glob(path))

    if matches:
        logger.info(f"{job.cute_id} stage out files: {matches}")
        with tempfile.TemporaryDirectory() as stagingdir:
            try:
                for f in matches: 
                    base = os.path.basename(f)
                    dst = os.path.join(stagingdir, base)
                    shutil.copyfile(src=f, dst=dst)
                    logger.info(f"staging {f} out for transfer")
                logger.info(f"transferring to {url_out}")
                transfer.stage_out(f"{stagingdir}/", f"{url_out}/")
            except Exception as e:
                message = f'Exception received during stage_out: {e}'
                raise BalsamTransitionError(message) from e
    job.state = 'JOB_FINISHED'
    logger.info(f'{job.cute_id} stage_out done')


def preprocess(job):
    logger.debug(f'{job.cute_id} in preprocess')

    # Get preprocesser exe
    preproc_app = job.preprocess
    if not preproc_app:
        job.state = 'PREPROCESSED'
        return

    if not os.path.exists(preproc_app.split()[0]):
        #TODO: look for preproc in the EXE directories
        message = f"Preprocessor {preproc_app} does not exist on filesystem"
        raise BalsamTransitionError(message)

    # Create preprocess-specific environment
    envs = job.get_envs()

    # Run preprocesser with special environment in job working directory
    out = os.path.join(job.working_directory, f"preprocess.log")
    with open(out, 'w') as fp:
        fp.write(f"# Balsam Preprocessor: {preproc_app}")
        fp.flush()
        try:
            args = preproc_app.split()
            logger.info(f"{job.cute_id} preprocess Popen {args}")
            proc = subprocess.Popen(args, stdout=fp,
                                    stderr=subprocess.STDOUT, env=envs,
                                    cwd=job.working_directory,
                                    )
            retcode = proc.wait(timeout=PREPROCESS_TIMEOUT_SECONDS)
            proc.communicate()
        except Exception as e:
            message = f"Preprocess failed: {e}"
            proc.kill()
            raise BalsamTransitionError(message) from e

    if retcode != 0:
        tail = get_tail(out)
        message = f"{job.cute_id} preprocess returned {retcode}:\n{tail}"
        raise BalsamTransitionError(message)

    job.state = 'PREPROCESSED'
    logger.info(f"{job.cute_id} preprocess done")

def postprocess(job, *, error_handling=False, timeout_handling=False):
    logger.debug(f'{job.cute_id} in postprocess')
    if error_handling and timeout_handling:
        raise ValueError("Both error-handling and timeout-handling is invalid")
    if error_handling: logger.info(f'{job.cute_id} handling RUN_ERROR')
    if timeout_handling: logger.info(f'{job.cute_id} handling RUN_TIMEOUT')

    # Get postprocesser exe
    postproc_app = job.postprocess

    # If no postprocesssor; move on (unless in error_handling mode)
    if not postproc_app:
        if error_handling:
            message = f"{job.cute_id} handle error: no postprocessor found!"
            raise BalsamTransitionError(message)
        elif timeout_handling:
            job.state = 'RESTART_READY'
            logger.warning(f'{job.cute_id} unhandled job timeout: marked RESTART_READY')
            return
        else:
            job.state = 'POSTPROCESSED',
            logger.info(f'{job.cute_id} no postprocess: skipped')
            return

    if not os.path.exists(postproc_app.split()[0]):
        #TODO: look for postproc in the EXE directories
        message = f"Postprocessor {postproc_app} does not exist on filesystem"
        raise BalsamTransitionError(message)

    # Create postprocess-specific environment
    envs = job.get_envs(timeout=timeout_handling, error=error_handling)

    # Run postprocesser with special environment in job working directory
    out = os.path.join(job.working_directory, f"postprocess.log")
    with open(out, 'w') as fp:
        fp.write(f"# Balsam Postprocessor: {postproc_app}\n")
        if timeout_handling: fp.write("# Invoked to handle RUN_TIMEOUT\n")
        if error_handling: fp.write("# Invoked to handle RUN_ERROR\n")
        fp.flush()
        
        try:
            args = postproc_app.split()
            logger.info(f"{job.cute_id} postprocess Popen {args}")
            proc = subprocess.Popen(args, stdout=fp,
                                    stderr=subprocess.STDOUT, env=envs,
                                    cwd=job.working_directory,
                                    )
            retcode = proc.wait(timeout=POSTPROCESS_TIMEOUT_SECONDS)
            proc.communicate()
        except Exception as e:
            message = f"Postprocess failed: {e}"
            proc.kill()
            raise BalsamTransitionError(message) from e
    
    if retcode != 0:
        tail = get_tail(out, nlines=30)
        message = f"{job.cute_id} postprocess returned {retcode}:\n{tail}"
        raise BalsamTransitionError(message)

    job.refresh_from_db()
    # If postprocessor handled error or timeout, it should have changed job's
    # state. If it failed to do this, mark FAILED.  Otherwise, POSTPROCESSED.
    if error_handling and job.state == 'RUN_ERROR':
        message = f"{job.cute_id} Error handling didn't fix job state: marking FAILED"
        raise BalsamTransitionError(message)

    if timeout_handling and job.state == 'RUN_TIMEOUT':
        message = f"{job.cute_id} Timeout handling didn't change job state: marking FAILED"
        raise BalsamTransitionError(message)

    if not (error_handling or timeout_handling):
        job.state = 'POSTPROCESSED'
    logger.info(f"{job.cute_id} postprocess done")


def handle_timeout(job):
    if job.post_timeout_handler:
        logger.debug(f'{job.cute_id} invoking postprocess with timeout_handling flag')
        postprocess(job, timeout_handling=True)
    else:
        raise BalsamTransitionError(f"{job.cute_id} no timeout handling: marking FAILED")


def handle_run_error(job):
    if job.post_error_handler:
        logger.debug(f'{job.cute_id} invoking postprocess with error_handling flag')
        postprocess(job, error_handling=True)
    else:
        raise BalsamTransitionError("No error handler: run failed")

TRANSITIONS = {
    'READY':            stage_in,
    'STAGED_IN':        preprocess,
    'RUN_DONE':         postprocess,
    'RUN_TIMEOUT':      handle_timeout,
    'RUN_ERROR':        handle_run_error,
    'POSTPROCESSED':    stage_out,
}


if __name__ == "__main__":
    count = BalsamJob.objects.all().count()
    assert isinstance(count, int)
    BalsamJob.objects.all().delete()
    BalsamJob.source.clear_stale_locks()
    [BalsamJob(name=f'job{i}').save() for i in range(10)]
    pool = TransitionProcessPool(1)
    time.sleep(500)
    pool.terminate()
