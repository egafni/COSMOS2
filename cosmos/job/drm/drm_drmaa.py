import os
import sys

from .DRM_Base import DRM
from .util import div, convert_size_to_kb

_drmaa_session = None

def get_drmaa_session():
    global _drmaa_session
    if _drmaa_session is None:
        import drmaa
        _drmaa_session = drmaa.Session()
        _drmaa_session.initialize()
    return _drmaa_session

class DRM_DRMAA(DRM):
    name = 'drmaa'
    _session = None

    def __init__(self, *args, **kwargs):
        super(DRM_DRMAA, self).__init__(*args, **kwargs)

    def submit_job(self, task):
        with get_drmaa_session().createJobTemplate() as jt:
            jt.remoteCommand = task.output_command_script_path
            jt.outputPath = ':' + task.output_stdout_path
            jt.errorPath = ':' + task.output_stderr_path
            jt.jobEnvironment = os.environ
            jt.nativeSpecification = task.drm_native_specification or ''

            try:
                drm_jobID = get_drmaa_session().runJob(jt)
            except BaseException:
                print >>sys.stderr, \
                    "Couldn't run task with uid=%s and nativeSpecification=%s" % \
                    (task.uid, jt.nativeSpecification)
                raise

        return drm_jobID

    def filter_is_done(self, tasks):
        import drmaa

        jobid_to_task = {t.drm_jobID: t for t in tasks}
        # Keep yielding jobs until timeout > 1s occurs or there are no jobs
        while len(jobid_to_task):

            failed_jobs = []

            try:
                # disable_stderr() #python drmaa prints whacky messages sometimes.  if the script just quits without printing anything, something really bad happend while stderr is disabled
                drmaa_jobinfo = get_drmaa_session().wait(jobId=drmaa.Session.JOB_IDS_SESSION_ANY, timeout=1)._asdict()
                # enable_stderr()

                yield jobid_to_task.pop(int(drmaa_jobinfo['jobId'])), \
                      parse_drmaa_jobinfo(drmaa_jobinfo)

            except drmaa.errors.ExitTimeoutException:
                # Jobs are queued, but none are done yet. Exit loop.
                # enable_stderr()
                break

            except drmaa.errors.InvalidJobException:
                # There are no jobs left to wait on!
                raise RuntimeError('Should not be waiting on non-existent jobs.')

            except Exception as exc:
                #
                # python-drmaa occasionally throws a naked Exception. Yuk!
                #
                # 'code 24' may occur when a running or queued job is killed.
                # If we see that, one or more jobs may be dead, but if so,
                # which one(s)? drmaa.Session.wait() hasn't returned a job id,
                # or much of anything.
                #
                # TODO This code correctly handles cases when a running job is
                # TODO killed, but killing a *queued* job (before it is even
                # TODO scheduled) can really foul things up, in ways I don't
                # TODO quite understand. We can find and flag the failed job,
                # TODO but subsequent calls to wait() either block indefinitely
                # TODO or throw a bunch of exceptions that kill the Cosmos
                # TODO process. Personally, I blame python-drmaa, but still, it
                # TODO would be nice to handle this error case more gracefully.
                #
                # TL;DR Don't kill queued jobs!!!
                #
                if not exc.message.startswith("code 24"):
                    # not sure we can handle other bare drmaa exceptions cleanly
                    raise

                # "code 24: no usage information was returned for the completed job"
                print >>sys.stderr, 'drmaa raised a naked Exception while ' \
                                    'fetching job status - an existing job may ' \
                                    'have been killed'
                #
                # Check the status of each outstanding job and fake
                # a failure status for any that have gone missing.
                #
                for jobid in jobid_to_task.keys():
                    try:
                        drmaa_jobstatus = get_drmaa_session().jobStatus(str(jobid))
                    except drmaa.errors.InvalidJobException:
                        drmaa_jobstatus = drmaa.JobState.FAILED
                    except Exception:
                        drmaa_jobstatus = drmaa.JobState.UNDETERMINED

                    if drmaa_jobstatus in (drmaa.JobState.DONE,
                                           drmaa.JobState.FAILED,
                                           drmaa.JobState.UNDETERMINED):
                        cosmos_jobinfo = create_empty_drmaa_jobinfo(os.EX_TEMPFAIL)
                        failed_jobs.append((jobid_to_task.pop(jobid), cosmos_jobinfo))

            for jobid, task in failed_jobs:
                yield jobid, task

    def drm_statuses(self, tasks):
        import drmaa

        def get_status(task):
            try:
                return self.decodestatus[get_drmaa_session().jobStatus(str(task.drm_jobID))] if task.drm_jobID is not None else '?'
            except drmaa.errors.InvalidJobException:
                return '?'
            except:
                return '??'

        return {task.drm_jobID: get_status(task) for task in tasks}

    def kill(self, task):
        "Terminates a task"
        import drmaa

        if task.drm_jobID is not None:
            get_drmaa_session().control(str(task.drm_jobID), drmaa.JobControlAction.TERMINATE)

    def kill_tasks(self, tasks):
        for t in tasks:
            self.kill(t)

    @property
    def decodestatus(self):
        import drmaa

        return {drmaa.JobState.UNDETERMINED: 'process status cannot be determined',
                drmaa.JobState.QUEUED_ACTIVE: 'job is queued and active',
                drmaa.JobState.SYSTEM_ON_HOLD: 'job is queued and in system hold',
                drmaa.JobState.USER_ON_HOLD: 'job is queued and in user hold',
                drmaa.JobState.USER_SYSTEM_ON_HOLD: 'job is queued and in user and system hold',
                drmaa.JobState.RUNNING: 'job is running',
                drmaa.JobState.SYSTEM_SUSPENDED: 'job is system suspended',
                drmaa.JobState.USER_SUSPENDED: 'job is user suspended',
                drmaa.JobState.DONE: 'job finished normally',
                drmaa.JobState.FAILED: 'job finished, but failed'}


def div(n, d):
    if d == 0.:
        return 1
    else:
        return n / d


def parse_drmaa_jobinfo(drmaa_jobinfo):
    d = drmaa_jobinfo['resourceUsage']
    cosmos_jobinfo = dict(
        exit_status=int(drmaa_jobinfo['exitStatus']),

        percent_cpu=div(float(d['cpu']), float(d['ru_wallclock'])),
        wall_time=float(d['ru_wallclock']),

        cpu_time=float(d['cpu']),
        user_time=float(d['ru_utime']),
        system_time=float(d['ru_stime']),

        avg_rss_mem=d['ru_ixrss'],
        max_rss_mem_kb=convert_size_to_kb(d['ru_maxrss']),
        avg_vms_mem_kb=None,
        max_vms_mem_kb=convert_size_to_kb(d['maxvmem']),

        io_read_count=int(float(d['ru_inblock'])),
        io_write_count=int(float(d['ru_oublock'])),
        io_wait=float(d['iow']),
        io_read_kb=float(d['io']),
        io_write_kb=float(d['io']),

        ctx_switch_voluntary=int(float(d['ru_nvcsw'])),
        ctx_switch_involuntary=int(float(d['ru_nivcsw'])),

        avg_num_threads=None,
        max_num_threads=None,

        avg_num_fds=None,
        max_num_fds=None,

        memory=float(d['mem']),
    )

    #
    # Wait, what? drmaa has two exit status fields? Of course, they don't always
    # agree when an error occurs. Worse, sometimes drmaa doesn't set exit_status
    # when a job is killed. We may not be able to get the exact exit code, but
    # at least we can guarantee it will be non-zero for any job that shows signs
    # of terminating in error.
    #
    if int(drmaa_jobinfo['exitStatus']) != 0 or \
       drmaa_jobinfo['hasSignal'] or \
       drmaa_jobinfo['wasAborted'] or \
       not drmaa_jobinfo['hasExited']:

        if cosmos_jobinfo['exit_status'] == 0:
            cosmos_jobinfo['exit_status'] = int(float(
                drmaa_jobinfo['resourceUsage']['exit_status']))
        if cosmos_jobinfo['exit_status'] == 0:
            cosmos_jobinfo['exit_status'] = os.EX_SOFTWARE

        cosmos_jobinfo['successful'] = False
    else:
        cosmos_jobinfo['successful'] = True

    return cosmos_jobinfo


def create_empty_drmaa_jobinfo(exit_status):

    return dict(
        exit_status=int(exit_status),
        successful=(int(exit_status) == 0),

        percent_cpu=0.0,
        wall_time=0.0,

        cpu_time=0.0,
        user_time=0.0,
        system_time=0.0,

        avg_rss_mem=0.0,
        max_rss_mem_kb=0.0,
        avg_vms_mem_kb=None,
        max_vms_mem_kb=0.0,

        io_read_count=0,
        io_write_count=0,
        io_wait=0.0,
        io_read_kb=0.0,
        io_write_kb=0.0,

        ctx_switch_voluntary=0,
        ctx_switch_involuntary=0,

        avg_num_threads=None,
        max_num_threads=None,
        avg_num_fds=None,
        max_num_fds=None,
        memory=0.0
    )
