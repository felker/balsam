'''The MPICommand subclasses express a template for system-specfic MPI calls. If
the Launcher detects a specific host type, the appropriate MPICommand class is
automatically used.  Otherwise, the DEFAULTMPICommand class is assigned at
module-load time, testing for MPICH or OpenMPI'''
import sys
from balsam import settings
import logging
logger = logging.getLogger(__name__)


class BalsamRunnerException(Exception): pass


class MPICommand(object):
    '''Base Class for creating ``mpirun`` command lines.

    System-specific commands are generated by subclasses that specify the
    command and argument names. MPICommand instances are callable; the relevant
    parameters are passed in as arguments and an MPI command is generated from a
    template and returned'''
    def __init__(self):
        self.mpi = ''
        self.nproc = ''
        self.ppn = ''
        self.env = ''
        self.cpu_binding = None
        self.threads_per_rank = None
        self.threads_per_core = None

    def worker_str(self, workers):
        return ""

    def env_str(self, envs):
        envstrs = (f'{self.env} {var}="{val}"' for var, val in envs.items())
        return " ".join(envstrs)

    def threads(self, cpu_affinity, thread_per_rank, thread_per_core):
        return ""

    def __call__(self, workers, *, app_cmd, num_ranks, ranks_per_node,
                 envs, cpu_affinity, threads_per_rank=1, threads_per_core=1,
                 mpi_flags=''):
        '''Build the mpirun/aprun/runjob command line string'''
        workers = self.worker_str(workers)
        envs = self.env_str(envs)
        thread_str = self.threads(cpu_affinity, threads_per_rank,
                                  threads_per_core)
        result = (f"{self.mpi} {self.nproc} {num_ranks} {self.ppn} "
                  f"{ranks_per_node} {envs} {workers} {thread_str} "
                  f"{mpi_flags} {app_cmd}")
        return result


class OpenMPICommand(MPICommand):
    '''Single node OpenMPI: ppn == num_ranks'''
    def __init__(self):
        self.mpi = 'mpirun'
        self.nproc = '-n'
        self.ppn = '-npernode'
        self.env = '-x'
        self.cpu_binding = None
        self.threads_per_rank = None
        self.threads_per_core = None

    def worker_str(self, workers):
        return ""

    def env_str(self, envs):
        envstrs = (f'{self.env} {var}="{val}"' for var, val in envs.items())
        return " ".join(envstrs)

    def threads(self, cpu_affinity, thread_per_rank, thread_per_core):
        return ""

    def __call__(self, workers, *, app_cmd, num_ranks, ranks_per_node, envs,
                 cpu_affinity, threads_per_rank=1, threads_per_core=1,
                 mpi_flags=''):
        '''Build the mpirun/aprun/runjob command line string'''
        workers = self.worker_str(workers)
        envs = self.env_str(envs)
        thread_str = self.threads(cpu_affinity, threads_per_rank,
                                  threads_per_core)
        result = (f"{self.mpi} {self.nproc} {num_ranks} {self.ppn} "
                  f"{ranks_per_node} {envs} {workers} {thread_str} "
                  f"{mpi_flags} {app_cmd}")
        return result


class BGQMPICommand(MPICommand):
    def __init__(self):
        self.mpi = 'runjob'
        self.nproc = '--np'
        self.ppn = '-p'
        self.env = '--envs' # VAR1=val1:VAR2=val2
        self.cpu_binding = None
        self.threads_per_rank = None
        self.threads_per_core = None

    def worker_str(self, workers):
        if len(workers) != 1:
            raise BalsamRunnerException("BGQ requires exactly 1 worker (sub-block)")
        worker = workers[0]
        shape, block, corner = worker.shape, worker.block, worker.corner
        return f"--shape {shape} --block {block} --corner {corner} "


class ThetaMPICommand(MPICommand):
    def __init__(self):
        # 64 independent jobs, 1 per core of a KNL node: -n64 -N64 -d1 -j1
        self.mpi = 'aprun'
        self.nproc = '-n'
        self.ppn = '-N'
        self.env = '-e'
        self.cpu_binding = '-cc'
        self.threads_per_rank = '-d'
        self.threads_per_core = '-j'

    def threads(self, affinity, thread_per_rank, thread_per_core):
        assert affinity in 'depth none'.split()
        result = f"{self.cpu_binding} {affinity} "
        if affinity == 'depth':
            assert thread_per_rank >= 1 and thread_per_core >= 1
            result += f"{self.threads_per_rank} {thread_per_rank} "
            result += f"{self.threads_per_core} {thread_per_core} "
        return result

    def worker_str(self, workers):
        if not workers:
            return ""
        return f"-L {','.join(str(worker.id) for worker in workers)}"


class MPICHCommand(MPICommand):
    def __init__(self):
        # 64 independent jobs, 1 per core of a KNL node: -n64 -N64 -d1 -j1
        self.mpi = 'mpirun'
        self.nproc = '-n'
        self.ppn = '--ppn'
        self.env = '--env'
        self.cpu_binding = None
        self.threads_per_rank = None
        self.threads_per_core = None

    def worker_str(self, workers):
        return ""


class CooleyMPICommand(MPICommand):
    def __init__(self):
        # 64 independent jobs, 1 per core of a KNL node: -n64 -N64 -d1 -j1
        self.mpi = 'mpirun'
        self.nproc = '-n'
        self.ppn = '--ppn'
        self.env = '--env'
        self.cpu_binding = None
        self.threads_per_rank = None
        self.threads_per_core = None

    def worker_str(self, workers):
        if not workers:
            return ""
        return f"--hosts {','.join(str(worker.id) for worker in workers)} "


class SlurmMPICommand(MPICommand):
    def __init__(self):
        # 64 independent jobs, 1 per core of a KNL node: -n64 -N64 -d1 -j1
        self.mpi = 'srun'
        self.nproc = '-n'
        self.ppn = '--ntasks-per-node'
        self.env = ''
        self.cpu_binding = None
        self.threads_per_rank = None
        self.threads_per_core = None

    def env_str(self, envs):
        return ''

    def worker_str(self, workers):
        if not workers:
            return ""
        num = len(workers)
        return f"--nodelist {','.join(str(worker.id) for worker in workers)} --nodes {num} "


# TODO(KGF): currently, user must supply exact name of class in this file. Add parsing of
# srun/mpirun/aprun and rename setting to MPIRUN_EXE?
MPIcmd = getattr(sys.modules[__name__], settings.MPIRUN_CLASS)
