from time import sleep
from django.utils import timezone
from django.conf import settings
import pytz
from loguru import logger

from agents.models import Agent
from .models import WinUpdate
from tacticalrmm.celery import app

logger.configure(**settings.LOG_CONFIG)


@app.task
def auto_approve_updates_task():
    # scheduled task that checks and approves updates daily

    agents = Agent.objects.all()
    online = [i for i in agents if i.status == "online"]

    for agent in online:

        # check for updates on agent
        check_for_updates_task.apply_async(
            queue="wupdate",
            kwargs={"pk": agent.pk, "wait": False, "auto_approve": True},
        )


@app.task
def check_agent_update_schedule_task():
    # scheduled task that installs updates on agents if enabled
    agents = Agent.objects.all()
    online = [i for i in agents if i.status == "online"]

    for agent in online:
        patch_policy = agent.get_patch_policy()

        # check if auto approval is enabled
        if (
            patch_policy.critical == "approve"
            or patch_policy.important == "approve"
            or patch_policy.moderate == "approve"
            or patch_policy.low == "approve"
            or patch_policy.other == "approve"
        ):
            now = None

            # If agent timezone isn't set fallback to server time
            timezone.activate(pytz.timezone(agent.timezone))
            now = timezone.localtime(timezone.now())

            # get schedule and compare to agent's time
            weekday = int(now.strftime("%w"))
            hour = int(now.strftime("%-H"))

            # check if patches were scheduled to run today
            if weekday in patch_policy.run_time_days:

                # check if patches are past due
                if patch_policy.run_time_hour < hour:

                    # check if patches were already run for this cycle
                    if (
                        agent.patches_last_installed
                        and int(agent.patches_last_installed.strftime("%w")) == weekday
                    ):
                        return

                    # initiate update on agent asynchronously and don't worry about ret code
                    agent.salt_api_async(func="win_agent.install_updates")
                    agent.patches_last_installed = now
                    agent.save(update_fields=["patches_last_installed"])


@app.task
def check_for_updates_task(pk, wait=False, auto_approve=False):

    if wait:
        sleep(70)

    agent = Agent.objects.get(pk=pk)
    ret = agent.salt_api_cmd(
        timeout=310,
        func="win_wua.list",
        arg="skip_installed=False",
    )

    if ret == "timeout" or ret == "error":
        return

    if isinstance(ret, str):
        err = ["unknown failure", "2147352567", "2145107934"]
        if any(x in ret.lower() for x in err):
            logger.warning(f"{agent.salt_id}: {ret}")
            return "failed"

    guids = []
    # this exception will trigger on win 10 2004 until I release new salt minion with the fix
    try:
        for k in ret.keys():
            guids.append(k)
    except Exception as e:
        logger.error(f"{agent.salt_id}: {str(e)}")
        return "failed 2004"

    for i in guids:
        # check if existing update install / download status has changed
        if WinUpdate.objects.filter(agent=agent).filter(guid=i).exists():

            update = WinUpdate.objects.filter(agent=agent).get(guid=i)

            # salt will report an update as not installed even if it has been installed if a reboot is pending
            # ignore salt's return if the result field is 'success' as that means the agent has successfully installed the update
            if update.result != "success":
                if ret[i]["Installed"] != update.installed:
                    update.installed = not update.installed
                    update.save(update_fields=["installed"])

                if ret[i]["Downloaded"] != update.downloaded:
                    update.downloaded = not update.downloaded
                    update.save(update_fields=["downloaded"])

        # otherwise it's a new update
        else:
            WinUpdate(
                agent=agent,
                guid=i,
                kb=ret[i]["KBs"][0],
                mandatory=ret[i]["Mandatory"],
                title=ret[i]["Title"],
                needs_reboot=ret[i]["NeedsReboot"],
                installed=ret[i]["Installed"],
                downloaded=ret[i]["Downloaded"],
                description=ret[i]["Description"],
                severity=ret[i]["Severity"],
            ).save()

    agent.delete_superseded_updates()

    # win_wua.list doesn't always return everything
    # use win_wua.installed to check for any updates that it missed
    # and then change update status to match
    installed = agent.salt_api_cmd(
        timeout=60, func="win_wua.installed", arg="kbs_only=True"
    )

    if installed == "timeout" or installed == "error":
        pass
    elif isinstance(installed, list):
        agent.winupdates.filter(kb__in=installed).filter(installed=False).update(
            installed=True, downloaded=True
        )

    # check if reboot needed. returns bool
    needs_reboot = agent.salt_api_cmd(timeout=30, func="win_wua.get_needs_reboot")

    if needs_reboot == "timeout" or needs_reboot == "error":
        pass
    elif isinstance(needs_reboot, bool) and needs_reboot:
        agent.needs_reboot = True
        agent.save(update_fields=["needs_reboot"])
    else:
        agent.needs_reboot = False
        agent.save(update_fields=["needs_reboot"])

    # approve updates if specified
    if auto_approve:
        agent.approve_updates()

    return "ok"
