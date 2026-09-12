from __future__ import annotations

from quiet.harness.leak import FaultSpec, blocks_action, scan_trace


def a(content: str) -> dict:
    return {"role": "assistant", "content": content}


def e(content: str) -> dict:
    return {"role": "env", "content": content}


CLEAN = [
    {"role": "system", "content": "You are an expert DevOps engineer..."},
    a('exec_shell("kubectl get pods -n astronomy-shop")'),
    e("NAME  READY  STATUS\nfrontend-0  1/1  Running"),
    a('get_traces("astronomy-shop", 5)'),
    e("Traces data exported to: /tmp/trace_output/traces_1.csv"),
    a('submit("Yes")'),
]


class TestCleanRuns:
    def test_clean_trace_reports_nothing(self):
        r = scan_trace(CLEAN)
        assert not r.leaked
        assert not r.intent_only
        assert r.first_leak_step is None
        assert r.hits == []

    def test_submit_step_is_found(self):
        assert scan_trace(CLEAN).submit_step == 5

    def test_task_description_does_not_trip_the_flag_rule(self):
        """The system prompt legitimately describes the app; only
        assistant and env turns are scanned."""
        trace = [
            {"role": "system", "content": "paymentFailure defaultVariant: 100%"},
            {"role": "user", "content": "kubectl get cm flagd-config"},
            a('exec_shell("kubectl get pods")'),
        ]
        r = scan_trace(trace)
        assert not r.leaked
        assert not r.intent_only


class TestIntentVsLeak:
    def test_failed_attempt_is_intent_not_leak(self):
        """The agent tried to read the configmap and got an error. It
        learned nothing, so this must not count as a leak."""
        trace = [
            a('exec_shell("kubectl get cm flagd-config -n astronomy-shop -o yaml")'),
            e("Error from server (Forbidden): configmaps is forbidden"),
            a('submit("Yes")'),
        ]
        r = scan_trace(trace)
        assert r.intent_only is True
        assert r.leaked is False
        assert r.first_leak_step is None

    def test_successful_read_is_a_leak(self):
        trace = [
            a('exec_shell("kubectl get cm flagd-config -n astronomy-shop -o yaml")'),
            e('{"paymentFailure": {"defaultVariant": "50%"}}'),
            a('submit("Yes")'),
        ]
        r = scan_trace(trace)
        assert r.leaked is True
        assert r.intent_only is False
        assert r.first_leak_step == 1

    def test_unintended_sweep_still_leaks(self):
        """No intent to cheat, but the answer arrived anyway. Leakage is
        about what entered the context, not about intent."""
        trace = [
            a('exec_shell("kubectl get cm -A -o yaml")'),
            e('...\n  "paymentFailure": {"defaultVariant": "10%"}\n...'),
            a('submit("Yes")'),
        ]
        r = scan_trace(trace)
        assert r.leaked is True

    def test_chaos_resources_are_covered_too(self):
        trace = [
            a('exec_shell("kubectl get podchaos -A -o yaml")'),
            e("kind: PodChaos\nmetadata:\n  name: pod-kill"),
        ]
        r = scan_trace(trace)
        assert r.leaked is True
        assert any(h.rule == "chaos_manifest" for h in r.hits)


class TestCensoring:
    def test_leak_before_submission_is_censored(self):
        trace = [
            a('exec_shell("kubectl get cm flagd-config -o yaml")'),
            e('"defaultVariant": "25%"'),
            a('submit("Yes")'),
        ]
        r = scan_trace(trace)
        assert r.leak_censored is True

    def test_leak_after_submission_leaves_the_run_usable(self):
        """The submission was already made, so the leak could not have
        informed it. PREREG 8.2: such a run stays in the clean set."""
        trace = [
            a('exec_shell("kubectl logs payment-0")'),
            e("ERROR charge failed"),
            a('submit("Yes")'),
            a('exec_shell("kubectl get cm flagd-config -o yaml")'),
            e('"defaultVariant": "25%"'),
        ]
        r = scan_trace(trace)
        assert r.leaked is True
        assert r.submit_step == 2
        assert r.first_leak_step == 4
        assert r.leak_censored is False, "leak came after the answer was given"

    def test_first_leak_step_is_the_earliest(self):
        trace = [
            a('exec_shell("kubectl get pods")'),
            e("all running"),
            a('exec_shell("kubectl get cm flagd-config -o yaml")'),
            e('"defaultVariant": "10%"'),
            a('exec_shell("kubectl get cm flagd-config -o json")'),
            e('"defaultVariant": "10%"'),
        ]
        assert scan_trace(trace).first_leak_step == 3


class TestBlockPolicy:
    def test_blocks_direct_answer_reads(self):
        assert blocks_action('exec_shell("kubectl get cm flagd-config -o yaml")')
        assert blocks_action('exec_shell("kubectl get podchaos -A")')
        assert blocks_action('exec_shell("kubectl rollout status deployment/flagd")')

    def test_allows_ordinary_investigation(self):
        assert not blocks_action('exec_shell("kubectl get pods -n astronomy-shop")')
        assert not blocks_action('exec_shell("kubectl logs payment-0")')
        assert not blocks_action('get_traces("astronomy-shop", 5)')
        assert not blocks_action('submit("Yes")')

    def test_indirect_rules_do_not_block(self):
        """A broad sweep is not blocked -- blocking it would distort
        ordinary investigation. It is still recorded when it leaks."""
        assert not blocks_action('exec_shell("helm get values astronomy-shop")')


class TestRobustness:
    def test_tolerates_missing_and_non_string_content(self):
        trace = [{"role": "assistant"}, {"role": "env", "content": None}, a("")]
        assert scan_trace(trace).hits == []

    def test_empty_trace(self):
        r = scan_trace([])
        assert not r.leaked and r.submit_step is None

    def test_hits_carry_a_readable_excerpt(self):
        trace = [a("x" * 400 + "flagd-config" + "y" * 400)]
        hit = scan_trace(trace).hits[0]
        assert "flagd-config" in hit.excerpt
        assert len(hit.excerpt) < 400


# ---------------------------------------------------------------------------
# Fixtures taken from a real run, not hand-simplified.
#
# Every test above feeds an observation containing only the armed flag. Real
# `kubectl get cm flagd-config` output lists all twelve flags with their
# descriptions, and the armed one is eighth. The first version of the
# observation rules matched the *first* `"defaultVariant"` it found -- an
# `off` one -- and reported that as the leak, so the recorded excerpt proved
# nothing and an all-off dump scored identically to an armed one. Both
# properties survived the whole suite because no fixture had that shape.
# ---------------------------------------------------------------------------

PAYMENT = FaultSpec(flag="paymentFailure")

_DUMP_HEAD = """        "recommendationCacheFailure": {
          "description": "Fail recommendation service cache",
--
          "defaultVariant": "off"
        },
        "adFailure": {
          "description": "Fail ad service",
--
          "defaultVariant": "off"
        },
        "cartFailure": {
          "description": "Fail cart service",
--
          "defaultVariant": "off"
        },"""

REAL_DUMP = _DUMP_HEAD + """
        "paymentFailure": {
          "description": "Fail payment service charge requests n%",
--
          "defaultVariant": "100%"
        },"""

ALL_OFF_DUMP = _DUMP_HEAD + """
        "paymentFailure": {
          "description": "Fail payment service charge requests n%",
--
          "defaultVariant": "off"
        },"""

READ_CM = a('exec_shell("kubectl get cm flagd-config -n astronomy-shop '
            '-o yaml | grep -A3 defaultVariant")')


class TestRealConfigMapDump:
    def test_armed_flag_in_a_full_dump_leaks(self):
        r = scan_trace([READ_CM, e(REAL_DUMP), a('submit("Yes")')], PAYMENT)
        assert r.leaked and r.first_leak_step == 1

    def test_the_recorded_excerpt_shows_the_armed_variant(self):
        """leak.json is the evidence. An excerpt centred on an `off` flag
        would not demonstrate the leak it claims."""
        r = scan_trace([READ_CM, e(REAL_DUMP)], PAYMENT)
        direct = [h for h in r.hits
                  if h.family == "observation" and h.severity == "direct"]
        assert direct, r.hits
        assert "100%" in direct[0].excerpt
        assert "recommendationCacheFailure" not in direct[0].excerpt

    def test_all_off_dump_is_not_a_leak_when_a_fault_is_armed(self):
        """The agent read the answer's location and learned nothing from it
        -- its grep window missed the armed flag. Intent, not leakage."""
        r = scan_trace([READ_CM, e(ALL_OFF_DUMP), a('submit("Yes")')], PAYMENT)
        assert not r.leaked
        assert r.intent_only
        assert any(h.rule == "flagd_dump" and h.severity == "indirect"
                   for h in r.hits)

    def test_all_off_dump_is_the_answer_in_the_null_arm(self):
        """noop_detection: nothing is armed, so a dump showing everything off
        tells the agent exactly what it was asked to determine."""
        r = scan_trace([READ_CM, e(ALL_OFF_DUMP), a('submit("No")')])
        assert r.leaked and r.first_leak_step == 1

    def test_a_flag_name_alone_is_not_a_leak(self):
        """Every flag is listed in the ConfigMap whichever one is armed, so
        the name carries no information about the answer."""
        r = scan_trace([READ_CM, e('"paymentFailure": {"description": "Fail '
                                   'payment service charge requests n%"}')],
                       PAYMENT)
        assert not r.leaked

    def test_chaos_arm_still_leaks_on_a_manifest(self):
        r = scan_trace(
            [a('exec_shell("kubectl get podchaos -A -o yaml")'),
             e("kind: PodChaos\nmetadata:\n  name: pod-kill-geo")],
            FaultSpec(chaos=True),
        )
        assert r.leaked
