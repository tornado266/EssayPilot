"""Resumable grading workflows. No rendering, routing or session-state access.

The caller owns the user-scoped caches. Valid generated results and committed
cloud IDs are recorded before the next fallible step, so a retry resumes work.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from src.cloud_store import CloudStoreError

if TYPE_CHECKING:
    from src.cloud_store import CloudUser, SupabaseStore


class GradingSettlementError(RuntimeError):
    def __init__(self, message: str, *, warnings: set[str] | None = None):
        super().__init__(message)
        self.warnings = set(warnings or ())


@dataclass(frozen=True)
class GradingPolicy:
    report_version: str
    scoring_version: str
    skill_version: str
    model: str


@dataclass(frozen=True)
class FirstDraftOperations:
    score: Callable
    teach: Callable
    score_snapshot: Callable
    save_report: Callable
    append_errors: Callable


@dataclass
class FirstDraftResult:
    package: dict[str, Any]
    cloud_ids: dict[str, str]
    scores: dict[str, Any]
    reused: bool
    saved_path: Any = None
    error_book_path: Any = None
    settled: bool = False
    warnings: set[str] = field(default_factory=set)


@dataclass
class SecondDraftResult:
    display: dict[str, Any]
    warnings: set[str] = field(default_factory=set)


def run_second_draft(
    *, draft_1: dict, draft_2_text: str, task_type: str, model: str,
    user_id: str, parent_run_id: str, attempt_id: str, cached_generation: dict,
    generate: Callable, save_report: Callable, save_training_record: Callable,
    count_words: Callable, scores_from_report: Callable, record_grading_event: Callable,
    persist: Callable | None = None, release: Callable | None = None,
) -> SecondDraftResult:
    """Finish generation, local backup and cloud settlement independently."""
    warnings: set[str] = set()
    try:
        package, progress = generate()
        structured = dict(package["structured"])
        scores = scores_from_report(structured)
        if not cached_generation.get("local_saved"):
            try:
                if not cached_generation.get("report_backed_up"):
                    save_report(
                        task_type=task_type, topic=draft_1["topic"], essay=draft_2_text,
                        report=str(package["report"]), word_count=count_words(draft_2_text),
                        user_id=user_id, examiner_data=structured,
                        grading_metadata={k: package[k] for k in (
                            "model", "prompt_version", "skill_version", "schema_version", "graded_at")},
                    )
                    cached_generation["report_backed_up"] = True
                cached_generation["training_path"] = save_training_record(
                    user_id=user_id, task_question=draft_1["topic"],
                    draft_1_text=draft_1["text"], draft_1_scores=draft_1["scores"],
                    draft_1_feedback=draft_1["feedback"], draft_2_text=draft_2_text,
                    draft_2_scores=scores, draft_2_feedback=str(package["report"]),
                    progress_report=progress,
                )
            except OSError:
                if persist is None:
                    raise
                warnings.add("local_backup_warning")
            else:
                cached_generation["local_saved"] = True
    except Exception:
        ticket = cached_generation.get("access_ticket")
        # A confirmed cloud write must never be undone by a later local failure.
        if ticket and release is not None and not cached_generation.get("cloud_ids"):
            try:
                release(ticket)
            except CloudStoreError:
                pass
            else:
                cached_generation.pop("access_ticket", None)
        raise
    linked_ids = dict(cached_generation.get("cloud_ids") or {})
    pending = False
    if persist is not None:
        try:
            linked_ids = persist(
                draft_1=draft_1, draft_2_text=draft_2_text, draft_2_package=package,
                draft_2_scores=scores, progress_report=progress, cached_generation=cached_generation,
            )
        except (CloudStoreError, AttributeError):
            pending = True
            warnings.add("settlement_pending")
    if not cached_generation.get("grading_event_recorded"):
        record_grading_event(
            user_id=user_id, overall_band=scores["Overall Band"],
            essay_word_count=count_words(draft_2_text), model_name=model,
        )
        cached_generation["grading_event_recorded"] = True
    return SecondDraftResult({
        "scores": scores, "report": str(package["report"]), "progress_report": progress,
        "path": cached_generation.get("training_path"), "text": draft_2_text,
        "grading_run_id": str(linked_ids.get("grading_run_id") or ""),
        "attempt_id": attempt_id, "settlement_pending": pending,
        "user_id": user_id, "parent_grading_run_id": parent_run_id,
    }, warnings)


def run_first_draft(
    store, user, *, topic: str, essay: str, fingerprint: str, word_count: int,
    cache_key: tuple[str, str, str], cache: dict, pending_accesses: dict,
    policy: GradingPolicy, operations: FirstDraftOperations,
    reserve: Callable | None = None, complete: Callable | None = None,
    release: Callable | None = None,
) -> FirstDraftResult:
    warnings: set[str] = set()
    cached_entry = cache.get(cache_key)
    package = None
    locked = None
    cloud_ids = {}
    reused = False
    ticket = dict(pending_accesses.get(cache_key) or {})
    if isinstance(cached_entry, dict):
        cached_score = cached_entry.get("scoring_package")
        if (
            isinstance(cached_score, dict)
            and cached_score.get("prompt_version") == policy.scoring_version
            and cached_score.get("skill_version") == policy.skill_version
            and isinstance(cached_score.get("scoring"), dict)
            and cached_entry.get("scoring_topic") == topic
            and cached_entry.get("scoring_essay") == essay
        ):
            locked = dict(cached_score)
        candidate = dict(cached_entry.get("package") or {})
        if candidate.get("prompt_version") == policy.report_version:
            package = candidate
            if user is not None and cached_entry.get("cloud_user_id") == user.id:
                cloud_ids = dict(cached_entry.get("cloud_ids") or {})
            reused = bool(package)
        elif (
            candidate.get("scoring_prompt_version") == policy.scoring_version
            and candidate.get("skill_version") == policy.skill_version
            and isinstance(candidate.get("scoring"), dict)
        ):
            locked = {
                "provider": candidate.get("provider") or "OpenAI",
                "model": candidate.get("model") or policy.model,
                "response_model": candidate.get("response_model"),
                "system_fingerprint": candidate.get("system_fingerprint"),
                "reasoning_effort": candidate.get("reasoning_effort") or "none",
                "prompt_version": policy.scoring_version,
                "skill_version": policy.skill_version,
                "scoring": candidate["scoring"], "usage": {},
            }
    if package is None and user is not None:
        try:
            saved = store.find_cached_grading(user, fingerprint, policy.report_version)
        except CloudStoreError:
            saved = None
            warnings.add("cloud_cache_warning")
        if saved:
            structured = dict(saved.get("report_json") or {})
            package = {
                "model": str(saved.get("model") or policy.model),
                "schema_version": str(structured.get("schema_version") or "2.0"),
                "prompt_version": str(saved.get("prompt_version") or ""),
                "skill_version": str(saved.get("skill_version") or ""),
                "graded_at": str(saved.get("created_at") or ""),
                "structured": structured,
                "report": str(saved.get("report_markdown") or ""), "usage": {},
            }
            cloud_ids = {"essay_id": str(saved.get("essay_id") or ""),
                         "grading_run_id": str(saved.get("id") or "")}
            reused = True
            cache[cache_key] = {}
        elif locked is None:
            try:
                saved_score = store.find_cached_scoring(user, fingerprint, policy.scoring_version)
            except CloudStoreError:
                saved_score = None
            saved_json = (saved_score or {}).get("report_json") or {}
            saved_decision = saved_json.get("locked_scoring_decision") if isinstance(saved_json, dict) else None
            if isinstance(saved_decision, dict):
                locked = {
                    "provider": "OpenAI", "model": str(saved_score.get("model") or policy.model),
                    "prompt_version": policy.scoring_version, "skill_version": policy.skill_version,
                    "scoring": saved_decision, "usage": {},
                }
    if package is None:
        if not ticket and reserve is not None:
            ticket = dict(reserve(fingerprint) or {})
            if ticket and not ticket.get("local"):
                pending_accesses[cache_key] = ticket
        try:
            if locked is None:
                locked = operations.score(task_type="Task 2", topic=topic, essay=essay)
            cache[cache_key] = {"scoring_package": locked, "scoring_topic": topic, "scoring_essay": essay}
            package = operations.teach(
                task_type="Task 2", topic=topic, essay=essay, locked_scoring_package=locked,
            )
        except Exception:
            if ticket and release is not None:
                try:
                    release(ticket)
                except (CloudStoreError, AttributeError):
                    pass  # An uncertain release must retain the same reservation.
                else:
                    pending_accesses.pop(cache_key, None)
            raise
        cache[cache_key] = {}
    entry = cache.setdefault(cache_key, {})
    entry.update(package=package, cloud_ids=cloud_ids, cloud_user_id=user.id if user else "")
    structured = dict(package["structured"])
    scores = operations.score_snapshot(structured)
    if user is not None:
        try:
            if not entry.get("report_backed_up"):
                entry["saved_path"] = operations.save_report(
                    task_type="Task 2", topic=topic, essay=essay, report=str(package["report"]),
                    word_count=word_count, user_id=user.id,
                    parsed_result={"ok": True, "data": {"overall_band": structured["overall_band"],
                        "criteria_scores": {k: v for k, v in scores.items() if k != "Overall Band"}},
                        "raw": package["report"], "error": ""},
                    examiner_data=structured,
                    grading_metadata={k: package[k] for k in (
                        "model", "prompt_version", "skill_version", "schema_version", "graded_at", "usage")},
                    content_hash=fingerprint,
                )
                entry["report_backed_up"] = True
            if not entry.get("errors_backed_up"):
                entry["error_book_path"] = operations.append_errors(
                    task_type="Task 2", topic=topic, report=str(package["report"]), user_id=user.id,
                )
                entry["errors_backed_up"] = True
        except OSError:
            warnings.add("local_backup_warning")
    if user is not None and not cloud_ids:
        try:
            cloud_ids = store.save_grading_cycle(
                user, question=topic, essay=essay, word_count=word_count,
                package=package, content_hash=fingerprint,
            )
        except CloudStoreError:
            warnings.add("cloud_save_warning")
    entry["cloud_ids"] = cloud_ids
    settled = False
    if ticket and complete is not None:
        run_id = str(cloud_ids.get("grading_run_id") or "")
        if ticket.get("kind") == "membership" and not run_id:
            raise GradingSettlementError(
                "报告已经生成，但云端保存尚未完成。再次提交会复用本次结果，不会重新调用模型。",
                warnings=warnings,
            )
        try:
            complete(ticket, run_id)
        except (CloudStoreError, AttributeError) as exc:
            warnings.add("first_report_settlement_warning")
            raise GradingSettlementError(
                "报告已经生成，但权益状态暂时无法确认。请稍后用相同内容重试；不会重新调用模型。",
                warnings=warnings,
            ) from exc
        pending_accesses.pop(cache_key, None)
        settled = True
    return FirstDraftResult(package, cloud_ids, scores, reused,
                            entry.get("saved_path"), entry.get("error_book_path"), settled, warnings)


def generate_second_draft(
    *,
    provider: str,
    model: str,
    task_type: str,
    topic: str,
    draft_1_text: str,
    draft_1_scores: dict[str, float | None],
    draft_2_text: str,
    cached_generation: dict[str, object],
    score: Callable, teach: Callable, compare: Callable,
    prepare_comparison: Callable, scores_from_report: Callable,
) -> tuple[dict[str, object], str]:
    """Generate Draft 2 teaching and comparison concurrently after score lock."""
    cached_package = cached_generation.get("package")
    cached_progress = cached_generation.get("progress_report")
    if isinstance(cached_package, dict):
        if cached_progress:
            return dict(cached_package), str(cached_progress)
        draft_2_scores = scores_from_report(dict(cached_package["structured"]))
        progress_report = compare(
            provider=provider,
            task_question=topic,
            draft_1_text=draft_1_text,
            draft_1_scores=draft_1_scores,
            draft_2_text=draft_2_text,
            draft_2_scores=draft_2_scores,
            model=model,
        )
        cached_generation["progress_report"] = progress_report
        return dict(cached_package), progress_report

    cached_scoring = cached_generation.get("scoring_package")
    if isinstance(cached_scoring, dict):
        scoring_package = dict(cached_scoring)
    else:
        scoring_package = score(
            task_type=task_type,
            topic=topic,
            essay=draft_2_text,
        )
        cached_generation["scoring_package"] = scoring_package

    draft_2_scores = scores_from_report(dict(scoring_package["structured"]))
    comparison_executor = None
    comparison_future = None
    comparison_setup_error = None
    if not cached_progress:
        try:
            comparison_client, comparison_provider_config = prepare_comparison(provider)
            comparison_executor = ThreadPoolExecutor(max_workers=1)
            comparison_future = comparison_executor.submit(
                compare,
                provider=provider,
                task_question=topic,
                draft_1_text=draft_1_text,
                draft_1_scores=draft_1_scores,
                draft_2_text=draft_2_text,
                draft_2_scores=draft_2_scores,
                model=model,
                client=comparison_client,
                provider_config=comparison_provider_config,
            )
        except Exception as exc:
            comparison_setup_error = exc

    branch_errors: list[Exception] = []
    try:
        try:
            cached_generation["package"] = teach(
                task_type=task_type,
                topic=topic,
                essay=draft_2_text,
                locked_scoring_package=scoring_package,
            )
        except Exception as exc:
            branch_errors.append(exc)
        if comparison_future is not None:
            try:
                cached_generation["progress_report"] = comparison_future.result()
            except Exception as exc:
                branch_errors.append(exc)
    finally:
        if comparison_executor is not None:
            comparison_executor.shutdown(wait=True)
    if comparison_setup_error is not None:
        branch_errors.append(comparison_setup_error)
    if branch_errors:
        raise branch_errors[0]

    draft_2_package = cached_generation.get("package")
    if not isinstance(draft_2_package, dict):
        raise RuntimeError("Draft 2 teaching feedback did not return a package.")
    return dict(draft_2_package), str(cached_generation.get("progress_report") or "")


def persist_second_draft(
    store: SupabaseStore,
    user: CloudUser,
    *,
    draft_1: dict[str, object],
    draft_2_text: str,
    draft_2_package: dict[str, object],
    draft_2_scores: dict[str, float | None],
    progress_report: str,
    cached_generation: dict[str, object],
    complete: Callable, count_words: Callable, submission_hash: Callable,
) -> dict[str, object]:
    """Persist once, then make uncertain retries perform settlement only."""
    linked_ids = (
        dict(cached_generation.get("cloud_ids") or {})
        if isinstance(cached_generation.get("cloud_ids"), dict)
        else {}
    )
    revised_run_id = str(linked_ids.get("grading_run_id") or "")
    if revised_run_id and cached_generation.get("settled"):
        return linked_ids
    saved_ticket = cached_generation.get("access_ticket")
    if not isinstance(saved_ticket, dict) or not str(saved_ticket.get("flow_id") or ""):
        raise CloudStoreError("第二稿云端保存缺少原始预留凭证。")
    if not revised_run_id:
        linked_ids = store.save_second_draft_result(
            user,
            grading_run_id=str(draft_1.get("grading_run_id") or ""),
            flow_id=str(saved_ticket.get("flow_id") or ""),
            question=str(draft_1.get("topic") or ""),
            content=draft_2_text,
            word_count=count_words(draft_2_text),
            content_hash=submission_hash(str(draft_1.get("topic") or ""), draft_2_text),
            package=draft_2_package,
            scores=draft_2_scores,
            progress_report=progress_report,
        )
        revised_run_id = str(linked_ids.get("grading_run_id") or "")
        if not revised_run_id:
            raise CloudStoreError("第二稿云端保存未返回可确认的批改记录。")
        # Cache the committed ids before quota settlement. A timeout while
        # completing the action must not insert either row again.
        cached_generation["cloud_ids"] = linked_ids

    if isinstance(saved_ticket, dict) and not cached_generation.get("settled"):
        complete(
            store,
            user,
            dict(saved_ticket),
            revised_grading_run_id=revised_run_id,
        )
        cached_generation["settled"] = True
        cached_generation.pop("access_ticket", None)
    return linked_ids
