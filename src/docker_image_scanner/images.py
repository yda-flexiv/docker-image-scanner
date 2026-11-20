"""Extract uses of the legacy build image without running configuration code."""

from __future__ import annotations

import re
from collections.abc import Iterator

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

TARGET = "hub.flexiv.cloud/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base"

_Record = tuple[str, int, str]
_Parts = tuple[str | None, ...]  # None is unresolved interpolation, not literal text.
_STR = "tag:yaml.org,2002:str"
_MERGE = "tag:yaml.org,2002:merge"
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_VAR = re.compile(rf"\$({_NAME})")
_TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
_DIGEST = r"[a-z0-9]+(?:[+._-][a-z0-9]+)*:[A-Za-z0-9=_-]+"
_IMAGE = re.compile(rf"{re.escape(TARGET)}(?::{_TAG})?(?:@{_DIGEST})?")


def _expansion(text: str, start: int) -> int:
    """Return the end of a variable, template or command substitution, or start."""
    char = text[start]
    if char == "`":
        end = text.find("`", start + 1)
        return len(text) if end == -1 else end + 1
    pair = text[start + 1 : start + 2]
    if pair in ("{", "("):
        closer = "}" if pair == "{" else ")"
        depth = 1
        for index in range(start + 2, len(text)):
            if text[index] == pair:
                depth += 1
            elif text[index] == closer:
                depth -= 1
                if depth == 0:
                    return index + 1
        return len(text)
    match = _VAR.match(text, start)
    if match:
        return match.end()
    # Shell special and positional parameters are also unresolved.
    if pair and pair in "0123456789?$!#*@-":
        return start + 2
    return start


def _template(text: str) -> _Parts:
    parts: list[str | None] = []
    index = literal = 0
    while index < len(text):
        if text[index] in "$`" and (end := _expansion(text, index)) > index:
            parts.extend((text[literal:index], None))
            index = literal = end
        else:
            index += 1
    parts.append(text[literal:])
    return tuple(parts)


def _matches_template(parts: _Parts, value: str) -> bool:
    prefix, *middle, suffix = [part for part in parts if part is not None]
    start, end = len(prefix), len(value) - len(suffix)
    if start > end or not value.startswith(prefix) or not value.endswith(suffix):
        return False
    for fragment in middle:
        pos = value.find(fragment, start, end)
        if pos == -1:
            return False
        start = pos + len(fragment)
    return True


def _could_reference_target(parts: _Parts) -> bool:
    """Match fixed fragments against the exact repository, not a substring.

    After the repository, approximate dynamic tag/digest syntax conservatively:
    a delimiter is needed and no qualifier may contain a slash or whitespace.
    """
    positions = {0}
    length = len(TARGET)
    for index, part in enumerate(parts):
        if part is None:
            if not positions:
                return False
            positions = set(range(min(positions), length + 1))
            # This expansion can supply both the end of the repo and a delimiter.
            suffix = "".join(value or "" for value in parts[index + 1 :])
            if re.fullmatch(r"[A-Za-z0-9_:.@+=-]*", suffix):
                return True
            continue
        next_positions = set()
        for pos in positions:
            remainder = TARGET[pos:]
            if remainder.startswith(part):
                next_positions.add(pos + len(part))
            elif part.startswith(remainder) and part[len(remainder)] in ":@":
                suffix = part[len(remainder) :] + "".join(
                    value or "" for value in parts[index + 1 :]
                )
                if re.fullmatch(r"[A-Za-z0-9_:.@+=-]*", suffix):
                    return True
        positions = next_positions
    return length in positions


def _record(
    parts: _Parts, line: int, kind: str, records: list[_Record], errors: list[str]
) -> None:
    if None in parts:
        if _could_reference_target(parts):
            errors.append(f"line {line}: unresolved {kind} image reference")
        return
    reference = "".join(value or "" for value in parts)
    if _IMAGE.fullmatch(reference):
        records.append((reference, line, kind))
    elif re.match(rf"{re.escape(TARGET)}(?:[:@]|\s|$)", reference):
        errors.append(f"line {line}: invalid {kind} image reference")


def _line(node: Node) -> int:
    return node.start_mark.line + 1


class _InvalidYaml(ValueError):
    def __init__(self, node: Node, context: str):
        super().__init__(f"line {_line(node)}: invalid pipeline {context}")


class _Pipeline:
    def __init__(self) -> None:
        self.records: list[_Record] = []
        self.errors: list[str] = []
        self.mappings: dict[Node, dict[str, Node]] = {}
        self.steps_seen: set[Node] = set()
        self.groups_seen: set[Node] = set()
        self.active: set[Node] = set()
        self.services_used: dict[str, Node] = {}

    def mapping(self, node: Node, ancestors: frozenset[Node] = frozenset()) -> dict[str, Node]:
        if node in self.mappings:
            return self.mappings[node]
        if not isinstance(node, MappingNode) or node in ancestors:
            raise _InvalidYaml(node, "mapping or merge")
        merged: dict[str, Node] = {}
        direct: dict[str, Node] = {}
        for key, value in node.value:
            if isinstance(key, ScalarNode) and key.tag == _MERGE:
                sources = value.value if isinstance(value, SequenceNode) else [value]
                for source in sources:
                    for name, item in self.mapping(source, ancestors | {node}).items():
                        merged.setdefault(name, item)
            elif isinstance(key, ScalarNode):
                direct[key.value] = value
            else:
                raise _InvalidYaml(key, "mapping key")
        self.mappings[node] = merged | direct
        return self.mappings[node]

    def image_parts(self, node: Node) -> tuple[_Parts, int]:
        if isinstance(node, MappingNode):
            name = self.mapping(node).get("name")
            if name is None:
                raise _InvalidYaml(node, "image name")
            node = name
        if not isinstance(node, ScalarNode) or node.tag != _STR or not node.value:
            raise _InvalidYaml(node, "image name")
        return _template(node.value), _line(node)

    def image(self, node: Node, kind: str) -> None:
        parts, line = self.image_parts(node)
        _record(parts, line, kind, self.records, self.errors)

    def step(self, node: Node) -> None:
        if node in self.steps_seen:
            return
        self.steps_seen.add(node)
        step = self.mapping(node)
        if "image" in step:
            try:
                self.image(step["image"], "pipeline-step")
            except _InvalidYaml as error:
                self.errors.append(str(error))
        if "services" in step:
            services = step["services"]
            if not isinstance(services, SequenceNode):
                raise _InvalidYaml(services, "step services")
            for service in services.value:
                if not isinstance(service, ScalarNode) or service.tag != _STR:
                    raise _InvalidYaml(service, "service name")
                self.services_used.setdefault(service.value, service)

    def steps(self, node: Node) -> None:
        if node in self.active:
            raise _InvalidYaml(node, "recursive step group")
        if node in self.groups_seen:
            return
        if not isinstance(node, SequenceNode):
            raise _InvalidYaml(node, "step group")
        self.active.add(node)
        try:
            for entry in node.value:
                try:
                    item = self.mapping(entry)
                    if "step" in item:
                        self.step(item["step"])
                    for name in ("parallel", "stage"):
                        if name in item:
                            group = item[name]
                            if isinstance(group, MappingNode):
                                group_steps = self.mapping(group).get("steps")
                                if group_steps is None:
                                    raise _InvalidYaml(group, "step group")
                                group = group_steps
                            self.steps(group)
                except _InvalidYaml as error:
                    self.errors.append(str(error))
        finally:
            self.active.remove(node)
        self.groups_seen.add(node)

    def scan(self, root: Node) -> None:
        top = self.mapping(root)
        if "image" in top:
            try:
                self.image(top["image"], "pipeline-global")
            except _InvalidYaml as error:
                self.errors.append(str(error))
        if "pipelines" not in top:
            return
        for name, node in self.mapping(top["pipelines"]).items():
            try:
                if name == "default":
                    self.steps(node)
                elif name in {"branches", "tags", "pull-requests", "custom", "merge-queues"}:
                    for steps in self.mapping(node).values():
                        try:
                            self.steps(steps)
                        except _InvalidYaml as error:
                            self.errors.append(str(error))
            except _InvalidYaml as error:
                self.errors.append(str(error))
        if not self.services_used:
            return
        definitions = self.mapping(top["definitions"]) if "definitions" in top else {}
        services = self.mapping(definitions["services"]) if "services" in definitions else {}
        for name, use in self.services_used.items():
            selector = _template(name)
            if None in selector:
                for candidate, definition in services.items():
                    if not _matches_template(selector, candidate):
                        continue
                    try:
                        image = self.mapping(definition).get("image")
                        if image is None or not _could_reference_target(self.image_parts(image)[0]):
                            continue
                    except _InvalidYaml:
                        pass  # The matched definition cannot be ruled out.
                    self.errors.append(f"line {_line(use)}: unresolved pipeline service selection")
                    break
                continue
            if name not in services:
                if name != "docker":  # Bitbucket's built-in service.
                    self.errors.append(f"line {_line(use)}: undefined pipeline service")
                continue
            try:
                service = self.mapping(services[name])
                if "image" in service:
                    self.image(service["image"], "pipeline-service")
            except _InvalidYaml as error:
                self.errors.append(str(error))


def pipeline_images(text: str) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Return literal target references at their 1-based YAML source lines."""
    try:
        root = yaml.compose(text, Loader=_LOADER)
    except (yaml.YAMLError, RecursionError) as error:
        mark = getattr(error, "problem_mark", None)
        location = f"line {mark.line + 1}: " if mark is not None else ""
        # PyYAML's exception text can contain source snippets and credentials.
        return [], [f"{location}invalid pipeline YAML"]
    scanner = _Pipeline()
    if root is not None:
        try:
            scanner.scan(root)
        except _InvalidYaml as error:
            scanner.errors.append(str(error))
        except RecursionError:
            scanner.errors.append("pipeline YAML nesting is too deep")
    return sorted(set(scanner.records), key=lambda item: (item[1], item[2], item[0])), list(
        dict.fromkeys(scanner.errors)
    )


_ASSIGN = re.compile(rf"(?:export[ \t]+)?({_NAME})=(.*)")
_WORD_TAIL = re.compile(r"(?:;[ \t]*)?(?:#.*)?$")
_BRACED = re.compile(rf"\$\{{({_NAME})(?:(:-|-)(.*))?\}}", re.DOTALL)
_FIELDS = {
    "DOCKER_IMAGE",
    "FLEXIV_DOCKER_REGISTRY",
    "_docker_registry_host",
    "_docker_hub_registry",
    "_docker_image_repo",
    "_docker_image_tag",
    "_docker_image_commit",
    "_docker_image_id",
    "_pipeline_file",
}
_REGISTRY = "FLEXIV_DOCKER_REGISTRY"
_REGISTRY_EXPR = rf"\$(?:{_REGISTRY}|\{{{_REGISTRY}\}})"
_REGISTRY_VAR = rf'(?:"{_REGISTRY_EXPR}"|{_REGISTRY_EXPR})'
_CASE = re.compile(rf"case\s+{_REGISTRY_VAR}\s+in\b")
_CASE_ARM = re.compile(r"(hub|acr|\*)\)\s*(.*)")
_IF = re.compile(r"(if|elif)\s+(.*?)\s*;\s*then(?:\s+(.*))?$")
_CONTROL = re.compile(r"(else|fi|esac|;;)(?:\s+#.*)?$")
_REGISTRY_TEST = re.compile(
    rf"(!?\s*)\[{{1,2}}\s+{_REGISTRY_VAR}\s+(==?|!=)\s+[\"']?(hub|acr)[\"']?\s+\]{{1,2}}"
)
_FUNCTION = re.compile(rf"(?:function\s+{_NAME}(?:\s*\(\s*\))?|{_NAME}\s*\(\s*\))\s*\{{")
_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
# Recognize the historical command body, not any command that happens to name the pipeline.
_AWK_IMAGE_READER = """if ! DOCKER_IMAGE=$(awk '
/^image:[[:space:]]*(#.*)?$/ { in_image = 1; next }
/^[^[:space:]#]/ { in_image = 0 }
in_image && /^[[:space:]]+name:[[:space:]]*/ {
sub(/^[[:space:]]+name:[[:space:]]*/, "")
sub(/[[:space:]]+#.*$/, "")
sub(/[[:space:]]+$/, "")
print
exit
}
' "$_pipeline_file"); then"""
_PIPELINE = object()


def _shell_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield lines outside heredocs, joining common shell continuations."""
    start = 0
    pending = ""
    heredoc: tuple[str, str] | None = None
    for no, raw in enumerate(text.splitlines(), 1):
        if heredoc:
            tabs, delimiter = heredoc
            if (raw.lstrip("\t") if tabs else raw) == delimiter:
                heredoc = None
            continue
        if not pending:
            start = no
        if raw.endswith("\\") and not raw.endswith("\\\\") and not raw.lstrip().startswith("#"):
            pending += raw[:-1]
            continue
        line, pending = (pending + raw).strip(), ""
        if line and not line.startswith("#"):
            yield start, line
            if "<<<" not in line and (match := _HEREDOC.search(line)):
                heredoc = match.group(1, 2)
    if pending:
        yield start, pending.strip()


def _shell_word(text: str, values: dict[str, _Parts]) -> _Parts:
    """Resolve shell assignment words in memory; opaque substitutions stay unknown."""
    quote = ""
    parts: list[str | None] = []
    literal: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if not quote and (char.isspace() or char == ";"):
            if not _WORD_TAIL.fullmatch(text[index:].lstrip()):
                return (None,)
            break
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        elif quote != "'" and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if quote != '"' or following in '$`"\\':
                index += 1
                literal.append(following)
            else:
                literal.append(char)
        elif quote != "'" and char in "$`" and (end := _expansion(text, index)) > index:
            parts.append("".join(literal))
            literal.clear()
            expr = text[index:end]
            variable = _VAR.fullmatch(expr)
            braced = _BRACED.fullmatch(expr)
            if variable:
                parts.extend(values.get(variable[1], (None,)))
            elif braced:
                name, op, default = braced.groups()
                value = values.get(name)
                if op and (value == ("",) and op == ":-" or value is None and name == _REGISTRY):
                    value = _shell_word(default, values)
                parts.extend(value if value is not None else (None,))
            else:
                parts.append(None)
            index = end
            continue
        elif not quote and char in ";&|()<>":
            return (None,)
        else:
            literal.append(char)
        index += 1
    if quote:
        return (None,)
    parts.append("".join(literal))
    if None not in parts:
        return ("".join(value or "" for value in parts),)
    return tuple(parts)


def _registry_condition(test: str, values: dict[str, _Parts]) -> bool | None:
    match = _REGISTRY_TEST.fullmatch(test)
    selected = values.get(_REGISTRY, ("hub",))
    if match is None or None in selected:
        return None
    negate, operator, name = match.groups()
    equal = selected == (name,)
    matches = not equal if operator == "!=" else equal
    return not matches if negate.strip() else matches


def _modern_block(lines: list[tuple[int, str]], start: int) -> int | None:
    expected = _AWK_IMAGE_READER.splitlines()
    source = [line for _, line in lines[start : start + len(expected)]]
    return start + len(expected) if source == expected else None


def docker_images(text: str) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Return the configured Hub image, never sourcing the shell or environment.

    Only literal assignments, basic variable interpolation, and the default
    Hub/ACR selection are modeled. Other commands are not interpreted.
    """
    lines = list(_shell_lines(text))
    if not lines:
        return [], []
    values: dict[str, _Parts] = {}
    image: _Parts | object | None = None
    image_line = lines[0][0]
    # (kind, active arm, whether an earlier or current if arm matched).
    branches: list[tuple[str, bool | None, bool | None]] = []
    function_depth = 0
    multiline_quote = ""
    index = 0
    while index < len(lines):
        no, line = lines[index]
        index += 1
        if multiline_quote:
            if multiline_quote in line:
                multiline_quote = ""
            continue
        if _FUNCTION.match(line):
            if not line.endswith("}"):
                function_depth += 1
            continue
        if function_depth:
            if line == "}":
                function_depth -= 1
            continue
        if line.startswith("if ! DOCKER_IMAGE=$(awk '"):
            end = _modern_block(lines, index - 1)
            path_part = values.get("_pipeline_file", ("",))[-1] or ""
            if end is not None and path_part.rsplit("/", 1)[-1] == "bitbucket-pipelines.yml":
                if all(taken is True for _, taken, _ in branches):
                    image, image_line = _PIPELINE, no
                branches.append(("if", None, None))
                index = end
                continue
            # Still account for the unsupported multiline substitution in the condition.
            multiline_quote = "'"
            if all(taken is not False for _, taken, _ in branches):
                image, image_line = (None,), no
            branches.append(("if", None, None))
            continue
        if _CASE.match(line):
            branches.append(("case", False, None))
            continue
        if line.startswith("case "):
            branches.append(("unknown-case", None, None))
            continue
        if branches and branches[-1][0] == "case" and (arm := _CASE_ARM.fullmatch(line)):
            name, line = arm.groups()
            selected = values.get(_REGISTRY, ("hub",))
            if name == "*":
                taken = selected not in {("hub",), ("acr",)}
            else:
                taken = selected == (name,)
            branches[-1] = ("case", None if None in selected else taken, None)
        if match := _IF.fullmatch(line):
            keyword, test, line = match.groups()
            condition = _registry_condition(test, values)
            if keyword == "if":
                branches.append(("if", condition, condition))
            elif branches and branches[-1][0] == "if":
                _, _, claimed = branches[-1]
                active = False if claimed is True or condition is False else condition if claimed is False else None
                any_match = True if condition is True or claimed is True else condition if claimed is False else claimed
                branches[-1] = ("if", active, any_match)
            if line is None:
                continue
        elif control := _CONTROL.fullmatch(line):
            keyword = control[1]
            if branches:
                kind, _, claimed = branches[-1]
                if keyword == "fi" and kind == "if" or keyword == "esac" and kind.endswith("case"):
                    branches.pop()
                elif keyword == "else" and kind == "if":
                    branches[-1] = (kind, None if claimed is None else not claimed, True)
                elif keyword == ";;" and kind == "case":
                    branches[-1] = (kind, False, None)
            continue
        close_case = re.search(r";;(?:\s+#.*)?$", line) if branches and branches[-1][0] == "case" else None
        statement = line[: close_case.start()].rstrip() if close_case else line
        active = [taken for _, taken, _ in branches]
        if False not in active and (match := _ASSIGN.fullmatch(statement)):
            field, rhs = match.groups()
            if field in _FIELDS:
                if field == "DOCKER_IMAGE" and image is _PIPELINE and rhs == '"${DOCKER_IMAGE:1:${#DOCKER_IMAGE}-2}"':
                    pass  # Known quote removal in the pipeline-reader implementation.
                else:
                    result = (None,) if None in active else _shell_word(rhs, values)
                    values[field] = result
                    if field == "DOCKER_IMAGE":
                        image, image_line = result, no
        if close_case:
            branches[-1] = ("case", False, None)
    if image is _PIPELINE:
        return [], []
    if not isinstance(image, tuple) or image == ("",):
        return [], [f"line {image_line}: unrecognized Docker image configuration"]
    records: list[_Record] = []
    errors: list[str] = []
    _record(image, image_line, "docker-config", records, errors)
    return records, errors
