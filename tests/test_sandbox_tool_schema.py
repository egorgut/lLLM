"""The `sandbox_execute` contract and argument validation (SPEC-016 §7, §21.1).

Two things are under test here, and the second matters more than the first.

The schema is the security boundary: if it accepted one extra property, a model
could start negotiating about images, mounts, or timeouts. So the tests assert
not only that the declared fields work, but that the fields a caller might wish
for are absent — there is no way to express them.

Validation is the delegation boundary. It rejects shape mistakes itself and
hands every limit and the whole relative-path policy to SPEC-015, so the two
layers can never disagree about what a legal input is.
"""

import base64

import pytest

from sandbox_runtime.models import SandboxLanguage
from sandbox_tool.schema import (
    SANDBOX_EXECUTE_SPEC,
    InvalidSandboxRequest,
    validate_arguments,
)
from support_sandbox import make_policy


@pytest.fixture
def policy(tmp_path):
    return make_policy(tmp_path)


def call(**kwargs):
    return kwargs


def _property_names(schema) -> set[str]:
    """Every property name declared anywhere in a JSON schema."""

    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names |= set(value)
            names |= _property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= _property_names(item)
    return names


class TestToolSpec:
    def test_is_a_plain_local_tool_named_sandbox_execute(self):
        assert SANDBOX_EXECUTE_SPEC.name == "sandbox_execute"
        # Not an MCP tool: MCP names carry the `mcp_<server>__` prefix.
        assert not SANDBOX_EXECUTE_SPEC.name.startswith("mcp_")

    def test_input_schema_forbids_additional_properties(self):
        assert SANDBOX_EXECUTE_SPEC.input_schema["additionalProperties"] is False

    def test_exposes_exactly_three_arguments(self):
        assert set(SANDBOX_EXECUTE_SPEC.input_schema["properties"]) == {
            "language",
            "source",
            "input_files",
        }
        assert SANDBOX_EXECUTE_SPEC.input_schema["required"] == ["language", "source"]

    def test_offers_no_operational_argument(self):
        """No field through which a model could reach Docker policy (§26).

        Checks the declared property *names* at every depth: a mention inside a
        description ("commands already in the image") is documentation, but a
        property called `image` would be a capability.
        """

        declared = _property_names(SANDBOX_EXECUTE_SPEC.input_schema)
        assert declared == {"language", "source", "input_files", "name", "content", "encoding"}
        assert not declared & {
            "image",
            "image_ref",
            "mounts",
            "volumes",
            "network",
            "env",
            "environment",
            "timeout",
            "memory",
            "cpus",
            "privileged",
            "user",
            "workdir",
            "docker_args",
        }

    def test_only_python_and_bash_are_offered(self):
        languages = SANDBOX_EXECUTE_SPEC.input_schema["properties"]["language"]["enum"]
        assert languages == ["python", "bash"]

    def test_input_file_entries_also_forbid_additional_properties(self):
        items = SANDBOX_EXECUTE_SPEC.input_schema["properties"]["input_files"]["items"]
        assert items["additionalProperties"] is False
        assert set(items["properties"]) == {"name", "content", "encoding"}


class TestLanguageAndSource:
    def test_python_is_accepted(self, policy):
        language, source, files = validate_arguments(
            call(language="python", source="print(1)"), policy=policy
        )
        assert language is SandboxLanguage.PYTHON
        assert source == "print(1)"
        assert files == {}

    def test_bash_is_accepted(self, policy):
        language, _, _ = validate_arguments(
            call(language="bash", source="echo hi"), policy=policy
        )
        assert language is SandboxLanguage.BASH

    @pytest.mark.parametrize("language", ["ruby", "PYTHON", "python3", "sh", ""])
    def test_other_languages_are_rejected(self, policy, language):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(call(language=language, source="x"), policy=policy)

    @pytest.mark.parametrize("source", ["", "   ", "\n\t "])
    def test_empty_source_is_rejected(self, policy, source):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(call(language="python", source=source), policy=policy)

    def test_source_with_nul_byte_is_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(
                call(language="python", source="print(1)\x00"), policy=policy
            )

    def test_non_string_source_is_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(call(language="python", source=42), policy=policy)


class TestArgumentShape:
    def test_unknown_argument_is_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest) as error:
            validate_arguments(
                call(language="python", source="x", image="alpine"), policy=policy
            )
        assert "image" in str(error.value)

    def test_missing_required_argument_is_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(call(source="print(1)"), policy=policy)

    def test_non_object_arguments_are_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments("language=python", policy=policy)

    def test_omitted_input_files_defaults_to_empty(self, policy):
        _, _, files = validate_arguments(
            call(language="python", source="x"), policy=policy
        )
        assert files == {}


class TestInputFiles:
    def test_utf8_content_is_encoded(self, policy):
        _, _, files = validate_arguments(
            call(
                language="python",
                source="x",
                input_files=[{"name": "data.csv", "content": "a,b\n1,2\n"}],
            ),
            policy=policy,
        )
        assert files == {"data.csv": b"a,b\n1,2\n"}

    def test_base64_content_is_decoded(self, policy):
        payload = base64.b64encode(b"\x00\x01\x02").decode()
        _, _, files = validate_arguments(
            call(
                language="python",
                source="x",
                input_files=[
                    {"name": "blob.bin", "content": payload, "encoding": "base64"}
                ],
            ),
            policy=policy,
        )
        assert files == {"blob.bin": b"\x00\x01\x02"}

    def test_invalid_base64_is_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest) as error:
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files=[
                        {"name": "b.bin", "content": "not base64!", "encoding": "base64"}
                    ],
                ),
                policy=policy,
            )
        assert "base64" in str(error.value)

    def test_unknown_encoding_is_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files=[
                        {"name": "a.txt", "content": "x", "encoding": "latin-1"}
                    ],
                ),
                policy=policy,
            )

    def test_duplicate_names_are_rejected(self, policy):
        """A list can carry the same name twice; a mapping silently could not."""

        with pytest.raises(InvalidSandboxRequest) as error:
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files=[
                        {"name": "a.txt", "content": "first"},
                        {"name": "a.txt", "content": "second"},
                    ],
                ),
                policy=policy,
            )
        assert "Duplicate" in str(error.value)

    def test_case_insensitive_duplicate_names_are_rejected(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files=[
                        {"name": "Data.csv", "content": "1"},
                        {"name": "data.csv", "content": "2"},
                    ],
                ),
                policy=policy,
            )

    @pytest.mark.parametrize(
        "name",
        [
            "/etc/passwd",
            "../secret",
            "a/../../secret",
            "C:\\Windows\\system.ini",
            "\\\\server\\share",
            ".",
            "..",
            "",
            "a//b.txt",
            "sub/../../out.txt",
            "with\x00nul",
            "main.py",  # the reserved source filename
            "main.sh",
        ],
    )
    def test_unsafe_names_are_rejected(self, policy, name):
        """Path policy is SPEC-015's; this proves the tool cannot bypass it."""

        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files=[{"name": name, "content": "x"}],
                ),
                policy=policy,
            )

    def test_nested_relative_names_are_accepted(self, policy):
        _, _, files = validate_arguments(
            call(
                language="python",
                source="x",
                input_files=[{"name": "sub/dir/data.csv", "content": "1"}],
            ),
            policy=policy,
        )
        assert "sub/dir/data.csv" in files

    def test_over_long_name_is_rejected_at_the_runtime_bound(self, tmp_path):
        """The length cap comes from the policy, not from a second constant."""

        narrow = make_policy(tmp_path, max_artifact_path_chars=64)
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files=[{"name": "a" * 65 + ".txt", "content": "x"}],
                ),
                policy=narrow,
            )

    def test_entry_must_be_an_object_with_known_fields(self, policy):
        for entry in (
            "data.csv",
            {"name": "a.txt"},
            {"content": "x"},
            {"name": "a.txt", "content": "x", "mode": "0777"},
            {"name": 1, "content": "x"},
            {"name": "a.txt", "content": 1},
        ):
            with pytest.raises(InvalidSandboxRequest):
                validate_arguments(
                    call(language="python", source="x", input_files=[entry]),
                    policy=policy,
                )

    def test_input_files_must_be_an_array(self, policy):
        with pytest.raises(InvalidSandboxRequest):
            validate_arguments(
                call(
                    language="python",
                    source="x",
                    input_files={"a.txt": "x"},
                ),
                policy=policy,
            )
