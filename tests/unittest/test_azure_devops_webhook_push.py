import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.config_loader import global_settings
from pr_agent.servers import azuredevops_server_webhook as webhook
from pr_agent.servers import utils as servers_utils

URL = "https://dev.azure.com/fabrikam/Fabrikam/_git/Fabrikam/pullrequest/1"
PUSH_SUMMARY = "Yuro Itaki updated the source branch of pull request 1 (Fix checkout) in Fabrikam"


@pytest.fixture(params=["1.0", "2.0"])
def updated_event(request):
    # The 1.0 summary follows an empirical Zulip Azure fixture; 2.0 compatibility here is synthetic.
    # Commit fields are populated for every update kind, including metadata-only changes.
    return {
        "id": "7642b104-6c09-449b-a44e-24d24ea31f25",
        "eventType": "git.pullrequest.updated",
        "publisherId": "tfs",
        "resourceVersion": request.param,
        "message": {"text": PUSH_SUMMARY + "\r\n" + URL, "html": "<p>Source branch updated</p>"},
        "detailedMessage": {"text": PUSH_SUMMARY + "\nNew commits were pushed."},
        "resource": {
            "repository": {
                "id": "278d5cd2-584d-4b63-824a-2ba458937249",
                "name": "Fabrikam",
                "project": {"name": "Fabrikam"},
                "remoteUrl": "https://dev.azure.com/fabrikam/DefaultCollection/_git/Fabrikam",
            },
            "pullRequestId": 1,
            "status": "active",
            "isDraft": False,
            "title": "Fix checkout",
            "description": "Handle checkout failures.",
            "sourceRefName": "refs/heads/fix-checkout",
            "targetRefName": "refs/heads/main",
            "lastMergeSourceCommit": {"commitId": "a" * 40},
            "lastMergeTargetCommit": {"commitId": "b" * 40},
            "commits": [{"commitId": "a" * 40, "comment": "Handle checkout failures"}],
            "reviewers": [{"id": "reviewer-guid", "vote": 0}],
        },
        "resourceContainers": {"collection": {"baseUrl": "https://dev.azure.com/fabrikam/"}},
    }


@pytest.fixture
def runtime(monkeypatch):
    settings = copy.deepcopy(global_settings)
    settings.set("azure_devops_server.handle_push_trigger", True)
    settings.set("azure_devops_server.push_commands", ["/review"])
    settings.set("azure_devops_server.pr_commands", ["/describe"])
    settings.set("config.disable_auto_feedback", False)
    agent = MagicMock()
    agent.handle_request = AsyncMock()
    repo_settings = MagicMock()
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(servers_utils, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook, "apply_repo_settings", repo_settings)
    monkeypatch.setattr(webhook, "PRAgent", MagicMock(return_value=agent))
    monkeypatch.setattr(servers_utils, "_push_trigger_states_by_ttl", {})
    monkeypatch.setattr(servers_utils, "_active_push_trigger_states", {})
    return SimpleNamespace(settings=settings, agent=agent, repo_settings=repo_settings)


def test_push_defaults_are_opt_in():
    assert global_settings.get("azure_devops_server.handle_push_trigger") is False
    assert global_settings.get("azure_devops_server.push_commands") == ["/review"]


async def test_push_runs_push_commands_not_created_commands(updated_event, runtime):
    log_context = {}
    response = await webhook.handle_request_azure(updated_event, log_context)

    runtime.agent.handle_request.assert_awaited_once_with(URL, ["/review"])
    assert runtime.repo_settings.call_args_list == [call(URL), call(URL)]
    assert log_context == {"event": "git.pullrequest.updated", "api_url": URL}
    assert response.status_code == 202
    assert runtime.settings.get("config.is_auto_command") is True


@pytest.mark.parametrize("summary", [
    "Jamal Hartnett updated pull request 1 (Fix checkout).",
    "Jamal Hartnett updated the title of pull request 1 (Fix checkout).",
    "Jamal Hartnett voted 10 on pull request 1 (Fix checkout).",
    "Jamal Hartnett added a reviewer to pull request 1 (Fix checkout).",
    "Jamal Hartnett updated the reviewers of pull request 1 (Fix checkout).",
    "Unknown update to pull request 1 (Fix checkout).",
    "Actor updated pull request 1 (X updated the source branch of pull request 42 (fake)) in Fabrikam",
    "Actor updated pull request 1 (X updated the source branch of pull request 1 (fake)) in Fabrikam",
    "Actor updated the title of pull request 1 (X updated the source branch of pull request 1 (fake)) in Fabrikam",
    "Jamal Hartnett updated the title of pull request 1.\n" + PUSH_SUMMARY,
    "\n" + PUSH_SUMMARY,
])
async def test_non_push_updates_ignore_commit_fields_and_other_text(updated_event, runtime, summary):
    updated_event["message"]["text"] = summary
    updated_event["message"]["html"] = PUSH_SUMMARY
    updated_event["resource"]["title"] = PUSH_SUMMARY
    updated_event["resource"]["description"] = PUSH_SUMMARY
    updated_event["resource"]["notificationType"] = "PushNotification"

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


async def test_push_summary_is_case_insensitive_and_only_needs_first_line(updated_event, runtime):
    updated_event["message"]["text"] = PUSH_SUMMARY.upper() + "\nAdditional details."

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_awaited_once_with(URL, ["/review"])


@pytest.mark.parametrize("summary", [
    " updated the source branch of pull request 1 (Fix checkout) in Fabrikam",
    "Yuro Itaki updated the source branch of pull request 42 (Fix checkout) in Fabrikam",
    "Yuro Itaki updated the source branch of pull request 10 (Fix checkout) in Fabrikam",
    "Yuro Itaki updated the source branch of pull request 1 without a title",
    "Yuro Itaki updated the source branch",
])
async def test_push_summary_requires_actor_and_matching_pr_identity(updated_event, runtime, summary):
    updated_event["message"]["text"] = summary

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


@pytest.mark.parametrize("message", [None, {}, [], 123, "updated the source branch", {"text": None},
                                         {"text": 123}, {"text": []}, {"text": ""}])
async def test_missing_or_malformed_message_is_ignored(updated_event, runtime, message):
    if message is None:
        del updated_event["message"]
    else:
        updated_event["message"] = message

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


@pytest.mark.parametrize("enabled", [False, "false", "0", 0, None, "invalid"])
async def test_disabled_push_trigger_is_ignored(updated_event, runtime, enabled):
    runtime.settings.set("azure_devops_server.handle_push_trigger", enabled)

    await webhook.handle_request_azure(updated_event, {})

    runtime.repo_settings.assert_called_once_with(URL)
    runtime.agent.handle_request.assert_not_awaited()


async def test_repo_settings_can_opt_in_and_select_push_commands(updated_event, runtime):
    runtime.settings.set("azure_devops_server.handle_push_trigger", False)

    def apply_settings(url):
        assert url == URL
        runtime.settings.set("azure_devops_server.handle_push_trigger", "true")
        runtime.settings.set("azure_devops_server.push_commands", "/describe")

    runtime.repo_settings.side_effect = apply_settings
    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_awaited_once_with(URL, ["/describe"])


@pytest.mark.parametrize("disabled", [True, "true"])
async def test_disable_auto_feedback_blocks_push_commands(updated_event, runtime, disabled):
    runtime.settings.set("config.disable_auto_feedback", disabled)

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()


async def test_string_false_auto_feedback_setting_still_allows_push(updated_event, runtime):
    runtime.settings.set("config.disable_auto_feedback", "false")

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_awaited_once_with(URL, ["/review"])


@pytest.mark.parametrize(("commands", "expected"), [
    ("/review", [["/review"]]),
    (["/describe", "/review"], [["/describe"], ["/review"]]),
    (["/review", "/improve"], [["/review"]]),
    ([], []),
    (None, []),
    (123, []),
    ([123, "/review"], [["/review"]]),
])
async def test_push_command_settings_are_normalized(updated_event, runtime, commands, expected):
    runtime.settings.set("azure_devops_server.push_commands", commands)

    await webhook.handle_request_azure(updated_event, {})

    assert runtime.agent.handle_request.await_args_list == [call(URL, command) for command in expected]


@pytest.mark.parametrize(("field", "value"), [
    ("status", "completed"), ("status", "abandoned"), ("status", None),
    ("isDraft", True), ("isDraft", "true"), ("isDraft", "unknown"),
])
async def test_inactive_or_draft_pr_is_ignored(updated_event, runtime, field, value):
    updated_event["resource"][field] = value

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


async def test_version_one_payload_without_draft_field_is_supported(updated_event, runtime):
    del updated_event["resource"]["isDraft"]

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_awaited_once_with(URL, ["/review"])


@pytest.mark.parametrize("version", [None, "3.0", 1])
async def test_unknown_resource_version_is_ignored(updated_event, runtime, version):
    updated_event["resourceVersion"] = version

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


@pytest.mark.parametrize("resource", [None, [], {}])
async def test_malformed_resource_is_ignored(updated_event, runtime, resource):
    updated_event["resource"] = resource

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


async def test_invalid_pr_identity_is_ignored(updated_event, runtime):
    del updated_event["resource"]["repository"]

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_not_awaited()
    runtime.repo_settings.assert_not_called()


async def test_updated_event_uses_existing_web_link_precedence(updated_event, runtime):
    href = "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullrequest/7"
    updated_event["resource"]["_links"] = {"web": {"href": href}}

    await webhook.handle_request_azure(updated_event, {})

    runtime.agent.handle_request.assert_awaited_once_with(
        href.replace("_apis/git/repositories", "_git"), ["/review"]
    )


async def test_waiting_push_reviews_revision_of_push_refused_during_wait(updated_event, runtime):
    # A runs, B waits for the slot, C is refused: B's run is the only one left to cover C's commits.
    current = {"revision": "A"}
    reviewed = []
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    def cache_provider(url):
        # Mirror apply_repo_settings: reuse the request's cached provider or build one at the current revision.
        providers = context.setdefault("git_provider", {})
        if url not in providers:
            providers[url] = SimpleNamespace(revision=current["revision"])

    async def handle_request(url, command):
        reviewed.append(context["git_provider"][url].revision)
        if len(reviewed) == 1:
            a_started.set()
            await release_a.wait()

    runtime.repo_settings.side_effect = cache_provider
    runtime.agent.handle_request.side_effect = handle_request

    async def deliver():
        with request_cycle_context({}):
            await webhook.handle_request_azure(copy.deepcopy(updated_event), {})

    task_a = asyncio.create_task(deliver())
    await a_started.wait()
    current["revision"] = "B"
    task_b = asyncio.create_task(deliver())
    for _ in range(20):
        await asyncio.sleep(0)
    current["revision"] = "C"
    await deliver()
    release_a.set()
    await asyncio.gather(task_a, task_b)

    assert reviewed == ["A", "C"]


async def test_push_slot_refusal_does_not_run_commands(updated_event, runtime, monkeypatch):
    @asynccontextmanager
    async def refuse_slot(key, *, allow_backlog, ttl):
        assert (key, allow_backlog, ttl) == (URL, True, 300)
        yield False

    slot = MagicMock(side_effect=refuse_slot)
    monkeypatch.setattr(webhook, "push_trigger_slot", slot)

    await webhook.handle_request_azure(updated_event, {})

    slot.assert_called_once_with(URL, allow_backlog=True, ttl=300)
    runtime.repo_settings.assert_called_once_with(URL)
    runtime.agent.handle_request.assert_not_awaited()
