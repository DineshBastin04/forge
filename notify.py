import logging
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")

_AGENT_CONTEXT = {
    "Device Reset Agent": (
        "scans for devices flagged with *Data Errors* in the WMS and automatically clears "
        "their employee assignments and fork locations to restore normal warehouse operations"
    ),
    "Unpick Agent": (
        "identifies order lines where a pick reversal was logged but the pick record "
        "remains active, and resets them to prevent inventory count discrepancies"
    ),
}


def _post(url: str, payload: dict) -> bool:
    for attempt in range(2):
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
            return True
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt == 0:
                logger.debug("Notification transient error, retrying (%s): %s", url[:40], e)
                continue
            logger.warning("Notification send failed after retry (%s): %s", url[:40], e)
        except Exception as e:
            logger.warning("Notification send failed (%s): %s", url[:40], e)
            return False
    return False


def _greeting_name(executed_by: str) -> str:
    if executed_by == "scheduler":
        return "Team"
    name = executed_by.replace(".", " ").replace("_", " ").split()[0]
    return name.capitalize()


def _run_narrative(agent: str, db_name: str, executed_by: str,
                   total: int, success: int, errors: int, warnings: int) -> str:
    """Return a plain-English paragraph explaining what happened and what (if anything) needs attention.
    Uses Slack *bold* markers — callers targeting Teams should .replace('*', '**') before use."""
    desc     = _AGENT_CONTEXT.get(agent, "processed warehouse records")
    run_type = "The scheduled run" if executed_by == "scheduler" else "The manual run"

    if errors > 0 and warnings > 0:
        outcome = (
            f"{run_type} processed *{total}* record(s) in *{db_name}*. "
            f"*{errors}* error(s) require manual attention and *{warnings}* record(s) were safely "
            f"skipped — no warehouse data was modified for skipped records."
        )
    elif errors > 0:
        outcome = (
            f"{run_type} processed *{total}* record(s) in *{db_name}* and encountered *{errors}* "
            f"error(s). Please review the details below and take manual action if needed."
        )
    elif warnings > 0:
        outcome = (
            f"{run_type} processed *{total}* record(s) in *{db_name}*. "
            f"*{warnings}* record(s) were safely skipped — no warehouse data was modified for those "
            f"records. Review may be needed if those devices or orders remain stuck."
        )
    elif success > 0:
        outcome = (
            f"{run_type} completed in *{db_name}* — all *{success}* record(s) processed successfully. "
            f"No further action required."
        )
    else:
        outcome = f"{run_type} found no records requiring attention in *{db_name}*."

    return f"The *{agent}* {desc}.\n\n{outcome}"


def _record_label(r: dict) -> str:
    if r.get("device_id"):
        return str(r["device_id"])
    if r.get("order_number") and r.get("item_number"):
        wh = f"[{r['wh_id']}] " if r.get("wh_id") else ""
        return f"{wh}{r['order_number']} / {r['item_number']}"
    if r.get("order_number"):
        return str(r["order_number"])
    if r.get("id"):
        return str(r["id"])
    return "unknown"


def _teams_run_card(agent: str, db_name: str, executed_by: str, results: list) -> dict:
    total    = len(results)
    success  = sum(1 for r in results if r.get("status") == "SUCCESS")
    errors   = sum(1 for r in results if r.get("status") == "ERROR")
    warnings = sum(1 for r in results if r.get("status") == "WARNING")

    name      = _greeting_name(executed_by)
    run_label = "Scheduled Run" if executed_by == "scheduler" else "Manual Run"
    # Teams uses **bold** instead of Slack's *bold*
    narrative = _run_narrative(agent, db_name, executed_by, total, success, errors, warnings).replace("*", "**")

    facts = [
        {"name": "Total Processed", "value": str(total)},
        {"name": "Successful",      "value": str(success)},
        {"name": "Errors",          "value": str(errors)},
        {"name": "Warnings",        "value": str(warnings)},
    ]

    card = {
        "@type":    "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary":  f"{agent} Run — {db_name}",
        "sections": [{
            "activityTitle":    f"Hi {name} — {agent} Report for **{db_name}**",
            "activitySubtitle": f"{run_label}" + ("" if executed_by == "scheduler" else f" by {executed_by}"),
            "activityText":     narrative,
            "facts": facts,
        }],
    }

    issues = [r for r in results if r.get("status") in ("ERROR", "WARNING")]
    if issues:
        lines = []
        for r in issues[:5]:
            label  = _record_label(r)
            msg    = r.get("message", "")
            prefix = "✗" if r.get("status") == "ERROR" else "⚠"
            lines.append(f"{prefix} **{label}**" + (f" — {msg}" if msg else ""))
        if len(issues) > 5:
            lines.append(f"…and {len(issues) - 5} more")
        card["sections"].append({
            "title": "Records with Issues",
            "text":  "\n\n".join(lines),
        })

    return card


def _slack_run_blocks(agent: str, db_name: str, executed_by: str, results: list) -> dict:
    total    = len(results)
    success  = sum(1 for r in results if r.get("status") == "SUCCESS")
    errors   = sum(1 for r in results if r.get("status") == "ERROR")
    warnings = sum(1 for r in results if r.get("status") == "WARNING")

    if errors > 0:
        status_icon = ":x:"
    elif warnings > 0:
        status_icon = ":warning:"
    else:
        status_icon = ":white_check_mark:"

    name      = _greeting_name(executed_by)
    narrative = _run_narrative(agent, db_name, executed_by, total, success, errors, warnings)
    run_footer = (
        "Scheduled auto-run"
        if executed_by == "scheduler"
        else f"Triggered manually by *{executed_by}*"
    )

    fields = [
        {"type": "mrkdwn", "text": f"*Total Processed*\n{total}"},
        {"type": "mrkdwn", "text": f"*Successful*\n{success}"},
        {"type": "mrkdwn", "text": f"*Errors*\n{errors}"},
        {"type": "mrkdwn", "text": f"*Warnings*\n{warnings}"},
    ]

    blocks = [
        {"type": "header",  "text": {"type": "plain_text", "text": f"{status_icon} {agent} — {db_name}"}},
        {"type": "section", "text": {"type": "mrkdwn",     "text": f"Hi *{name}*,\n\n{narrative}"}},
        {"type": "divider"},
        {"type": "section", "fields": fields},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"{run_footer} · *{db_name}*"}
        ]},
    ]

    issues = [r for r in results if r.get("status") in ("ERROR", "WARNING")]
    if issues:
        lines = []
        for r in issues[:5]:
            label      = _record_label(r)
            msg        = r.get("message", "")
            issue_icon = ":x:" if r.get("status") == "ERROR" else ":warning:"
            lines.append(f"{issue_icon} *`{label}`*" + (f" — {msg}" if msg else ""))
        if len(issues) > 5:
            lines.append(f"_…and {len(issues) - 5} more_")
        blocks.insert(4, {"type": "divider"})
        blocks.insert(5, {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Records with Issues:*\n" + "\n".join(lines)},
        })

    return {"blocks": blocks}


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

    hex_color   = "FF0000" if level == "ERROR" else "FFA500"
    action_note = (
        "This error may require manual intervention to resolve."
        if level == "ERROR"
        else "Affected records were safely skipped — no warehouse data was modified."
    )

    teams_payload = {
        "@type": "MessageCard", "@context": "https://schema.org/extensions",
        "summary":    f"{level}: {agent} — {db_name}",
        "themeColor": hex_color,
        "sections": [{
            "activityTitle":    f"{'⚠ Warning' if level == 'WARNING' else '✗ Error'}: **{agent}** — **{db_name}**",
            "activitySubtitle": message,
            "activityText":     action_note,
        }],
    }

    icon = ":x:" if level == "ERROR" else ":warning:"
    slack_payload = {
        "attachments": [{
            "color": f"#{hex_color}",
            "blocks": [
                {"type": "section", "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{icon} *{level}* — *{agent}* in *{db_name}*\n\n"
                        f"{message}\n\n"
                        f"_{action_note}_"
                    ),
                }},
            ],
        }]
    }

    if teams_url:
        _executor.submit(_post, teams_url, teams_payload)
    if slack_url:
        _executor.submit(_post, slack_url, slack_payload)


def send_test(cfg: dict, channel: str) -> bool:
    notify  = cfg.get("notify", {})
    db_name = cfg.get("name", "Test")
    if channel == "teams":
        url = notify.get("teams_webhook", "")
        if not url:
            return False
        payload = {
            "@type":   "MessageCard",
            "summary": "Tychons WAI Agents — Webhook Test",
            "sections": [{
                "activityTitle":    "Tychons WAI Agents — Webhook Test",
                "activitySubtitle": f"Webhook verified for **{db_name}**",
                "activityText":     "Run reports and alerts for this site will be delivered to this channel.",
            }],
        }
        return _post(url, payload)
    if channel == "slack":
        url = notify.get("slack_webhook", "")
        if not url:
            return False
        payload = {
            "blocks": [
                {"type": "header",  "text": {"type": "plain_text", "text": ":bell: Tychons WAI Agents — Webhook Test"}},
                {"type": "section", "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Webhook is connected and working for *{db_name}*.\n\n"
                        f"Run reports and alerts for this site will be delivered to this channel."
                    ),
                }},
            ]
        }
        return _post(url, payload)
    return False
