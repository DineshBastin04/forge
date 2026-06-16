import logging
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")


def _post(url: str, payload: dict) -> bool:
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Notification send failed (%s): %s", url[:40], e)
        return False


def _teams_run_card(agent: str, db_name: str, executed_by: str, results: list) -> dict:
    total   = len(results)
    success = sum(1 for r in results if r.get("status") == "SUCCESS")
    errors  = sum(1 for r in results if r.get("status") == "ERROR")
    return {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary":  f"{agent} Run — {db_name}",
        "sections": [{
            "activityTitle":    f"{agent} — {db_name}",
            "activitySubtitle": f"Executed by: {executed_by}",
            "facts": [
                {"name": "Total",   "value": str(total)},
                {"name": "Success", "value": str(success)},
                {"name": "Errors",  "value": str(errors)},
            ],
        }],
    }


def _slack_run_blocks(agent: str, db_name: str, executed_by: str, results: list) -> dict:
    total   = len(results)
    success = sum(1 for r in results if r.get("status") == "SUCCESS")
    errors  = sum(1 for r in results if r.get("status") == "ERROR")
    return {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{agent} — {db_name}"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Total:* {total}"},
                {"type": "mrkdwn", "text": f"*Success:* {success}"},
                {"type": "mrkdwn", "text": f"*Errors:* {errors}"},
                {"type": "mrkdwn", "text": f"*By:* {executed_by}"},
            ]},
        ],
    }


def send_run_report(db_config_id: str, agent: str, results: list, executed_by: str = "scheduler"):
    from db_config import get_config
    cfg = get_config(db_config_id)
    if not cfg:
        return
    notify = cfg.get("notify", {})
    if not notify.get("report_after_run", False):
        return
    db_name   = cfg.get("name", db_config_id)
    teams_url = notify.get("teams_webhook", "")
    slack_url = notify.get("slack_webhook", "")
    if teams_url:
        _executor.submit(_post, teams_url, _teams_run_card(agent, db_name, executed_by, results))
    if slack_url:
        _executor.submit(_post, slack_url, _slack_run_blocks(agent, db_name, executed_by, results))


def send_alert(db_config_id: str, agent: str, level: str, message: str):
    from db_config import get_config
    cfg = get_config(db_config_id)
    if not cfg:
        return
    notify = cfg.get("notify", {})
    if level == "ERROR" and not notify.get("on_error", True):
        return
    if level == "WARNING" and not notify.get("on_warning", True):
        return
    db_name   = cfg.get("name", db_config_id)
    teams_url = notify.get("teams_webhook", "")
    slack_url = notify.get("slack_webhook", "")
    color = "FF0000" if level == "ERROR" else "FFA500"
    teams_payload = {
        "@type": "MessageCard", "@context": "https://schema.org/extensions",
        "summary":    f"{level}: {agent} — {db_name}",
        "themeColor": color,
        "sections":   [{"activityTitle": f"{level}: {agent} — {db_name}", "activitySubtitle": message}],
    }
    slack_payload = {
        "blocks": [{"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"*{level}* — {agent} ({db_name})\n{message}",
        }}],
    }
    if teams_url:
        _executor.submit(_post, teams_url, teams_payload)
    if slack_url:
        _executor.submit(_post, slack_url, slack_payload)


def send_test(cfg: dict, channel: str) -> bool:
    notify = cfg.get("notify", {})
    db_name = cfg.get("name", "Test")
    if channel == "teams":
        url = notify.get("teams_webhook", "")
        if not url:
            return False
        payload = {
            "@type": "MessageCard",
            "summary": f"Tychons Forge — Test",
            "sections": [{"activityTitle": "Tychons Forge — Test",
                          "activitySubtitle": f"Webhook verified for {db_name}"}],
        }
        return _post(url, payload)
    if channel == "slack":
        url = notify.get("slack_webhook", "")
        if not url:
            return False
        payload = {"blocks": [{"type": "section", "text": {
            "type": "mrkdwn",
            "text": f"*Tychons Forge — Test*\nWebhook verified for {db_name}",
        }}]}
        return _post(url, payload)
    return False
