"""Static checks on the dose-ladder wiring into AIOpsLab's registry.

Verified by parsing the source rather than importing it. Importing
``aiopslab.orchestrator.problems.registry`` drags in tiktoken, wandb,
pandas, prometheus_api_client and elasticsearch, and further down
``AstronomyShop.__init__`` calls ``create_namespace()`` -- so a laptop
cannot import it at all, let alone instantiate a problem.

These are exactly the mistakes that would otherwise surface on the lab
machine, at the start of a campaign: a forgotten star-import, a typo'd
problem id, a bare class registered where the constructor needs an
argument, or a variant name the chart does not actually offer.
"""

from __future__ import annotations

import ast
import json
import pytest

from quiet.paths import AIOPSLAB_ROOT as REPO  # the pinned submodule checkout
REGISTRY = REPO / "aiopslab" / "orchestrator" / "problems" / "registry.py"
PACKAGE = REPO / "aiopslab" / "orchestrator" / "problems" / "payment_failure_dose"
FLAGD_JSON = (
    REPO
    / "aiopslab-applications"
    / "astronomy-shop"
    / "charts"
    / "opentelemetry-demo"
    / "flagd"
    / "demo.flagd.json"
)

EXPECTED_DETECTION = {
    "payment_dose_00_sham-detection-1": "off",
    "payment_dose_10-detection-1": "10%",
    "payment_dose_25-detection-1": "25%",
    "payment_dose_50-detection-1": "50%",
    "payment_dose_75-detection-1": "75%",
    "payment_dose_90-detection-1": "90%",
    "payment_dose_100-detection-1": "100%",
}


def registry_tree() -> ast.Module:
    return ast.parse(REGISTRY.read_text())


def dose_entries() -> dict[str, ast.expr]:
    """Every registry entry whose key starts with ``payment_dose``."""
    out: dict[str, ast.expr] = {}
    for node in ast.walk(registry_tree()):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value.startswith("payment_dose")
            ):
                out[key.value] = value
    return out


class TestStarImport:
    def test_package_is_star_imported(self):
        """Without this line the classes are undefined at registry
        construction and every problem id fails to resolve."""
        imports = {
            node.module
            for node in ast.walk(registry_tree())
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert (
            "aiopslab.orchestrator.problems.payment_failure_dose" in imports
        ), "registry.py must star-import the dose package"

    def test_package_exports_what_the_registry_uses(self):
        exported = {
            alias.name
            for node in ast.walk(ast.parse((PACKAGE / "__init__.py").read_text()))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "PaymentFailureDoseDetection" in exported
        assert "PaymentFailureDoseLocalization" in exported


class TestProblemIds:
    def test_all_detection_doses_registered(self):
        assert set(EXPECTED_DETECTION) <= set(dose_entries())

    def test_ids_contain_their_task_type(self):
        """get_problem_ids() is a naive substring match, so the task type
        has to appear literally in the id or the problem is invisible."""
        for pid in dose_entries():
            assert (
                "detection" in pid or "localization" in pid
            ), f"{pid} would never be listed by get_problem_ids()"

    def test_no_duplicate_ids(self):
        keys = [
            key.value
            for node in ast.walk(registry_tree())
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        duplicates = {k for k in keys if keys.count(k) > 1}
        assert not duplicates, f"duplicate problem ids silently shadow: {duplicates}"

    def test_sham_has_no_localization_arm(self):
        """Nothing is broken in the sham arm, so there is no service to
        name. The class refuses it; the registry must not offer it."""
        assert not any(
            "sham" in pid and "localization" in pid for pid in dose_entries()
        )


class TestFactories:
    def test_every_dose_entry_is_a_zero_arg_lambda(self):
        """ProblemRegistry calls the registered value with no arguments.
        A bare class would raise TypeError, since the constructor needs a
        variant."""
        for pid, value in dose_entries().items():
            assert isinstance(value, ast.Lambda), f"{pid} must be a lambda"
            assert not value.args.args, f"{pid} lambda must take no arguments"

    def test_each_id_passes_the_variant_its_name_claims(self):
        """A mismatch here would silently mislabel an entire arm -- the
        worst possible failure, because the data would look fine."""
        for pid, value in dose_entries().items():
            if pid not in EXPECTED_DETECTION:
                continue
            call = value.body
            assert isinstance(call, ast.Call)
            kwargs = {
                kw.arg: kw.value.value
                for kw in call.keywords
                if isinstance(kw.value, ast.Constant)
            }
            assert kwargs.get("variant") == EXPECTED_DETECTION[pid], (
                f"{pid} passes variant={kwargs.get('variant')!r}, "
                f"expected {EXPECTED_DETECTION[pid]!r}"
            )


@pytest.fixture(scope="module")
def flagd_variants() -> set[str]:
    if not FLAGD_JSON.exists():
        pytest.skip("astronomy-shop submodule not checked out")
    data = json.loads(FLAGD_JSON.read_text())
    return set(data["flags"]["paymentFailure"]["variants"])


class TestVariantsExistInTheChart:
    """The ladder is only real if flagd actually offers these variants.

    Note this reads the VENDORED chart in the submodule, while the
    orchestrator installs astronomy-shop from the remote Helm repo with
    no version pin. The two can diverge, which is why inject_fault also
    validates against the live ConfigMap at injection time.
    """

    def test_every_registered_variant_is_offered(self, flagd_variants):
        for pid, variant in EXPECTED_DETECTION.items():
            assert variant in flagd_variants, (
                f"{pid} uses variant {variant!r}, which demo.flagd.json does "
                f"not define. Offered: {sorted(flagd_variants)}"
            )

    def test_dose_variants_constant_matches_the_chart(self, flagd_variants):
        source = (PACKAGE / "payment_failure_dose.py").read_text()
        tree = ast.parse(source)
        declared: tuple[str, ...] = ()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "DOSE_VARIANTS"
            ):
                declared = tuple(
                    e.value for e in node.value.elts if isinstance(e, ast.Constant)
                )
        assert declared, "DOSE_VARIANTS not found"
        assert set(declared) <= flagd_variants, (
            f"DOSE_VARIANTS has entries the chart does not offer: "
            f"{sorted(set(declared) - flagd_variants)}"
        )


class TestChartIsPinned:
    """An unpinned remote chart makes results irreproducible and, right
    now, breaks two upstream problems outright: 0.41.0 renamed
    loadGeneratorFloodHomepage, so injecting it raises 'flag not found'."""

    def test_astronomy_shop_pins_a_chart_version(self):
        meta = json.loads(
            (REPO / "aiopslab" / "service" / "metadata" / "astronomy-shop.json").read_text()
        )
        version = meta["Helm Config"].get("version")
        assert version, "astronomy-shop must pin a chart version"

    def test_pin_matches_the_vendored_submodule_chart(self):
        """The vendored chart is what the repo's own flagd config -- and
        every test that reads it -- was written against. If the pin and
        the submodule disagree, one of them is lying about what runs."""
        chart_yaml = (
            REPO
            / "aiopslab-applications"
            / "astronomy-shop"
            / "charts"
            / "opentelemetry-demo"
            / "Chart.yaml"
        )
        if not chart_yaml.exists():
            pytest.skip("astronomy-shop submodule not checked out")
        vendored = next(
            line.split(":", 1)[1].strip()
            for line in chart_yaml.read_text().splitlines()
            if line.startswith("version:")
        )
        meta = json.loads(
            (REPO / "aiopslab" / "service" / "metadata" / "astronomy-shop.json").read_text()
        )
        assert meta["Helm Config"]["version"] == vendored


class TestInjectorSupportsVariants:
    def test_inject_fault_takes_a_variant_argument(self):
        """Without this the ladder collapses: upstream hardcodes 100%."""
        source = (
            REPO / "aiopslab" / "generators" / "fault" / "inject_otel.py"
        ).read_text()
        tree = ast.parse(source)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "inject_fault"
        )
        arg_names = [a.arg for a in fn.args.args]
        assert "variant" in arg_names

    def test_variant_is_validated_against_the_live_configmap(self):
        """The chart is pulled unpinned from a remote Helm repo, so an
        unrecognised variant must fail loudly rather than write a value
        flagd will ignore."""
        source = (
            REPO / "aiopslab" / "generators" / "fault" / "inject_otel.py"
        ).read_text()
        assert "not in available" in source
        assert "Available:" in source
