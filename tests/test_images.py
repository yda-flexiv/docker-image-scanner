import unittest
from textwrap import dedent

from docker_image_scanner.images import TARGET, docker_images, pipeline_images


class PipelineImageTests(unittest.TestCase):
    def test_exact_references_preserve_tag_digest_and_line(self):
        digest = "sha256:" + "ab" * 32
        text = f"""image: {TARGET}:v3.1
pipelines:
  default:
    - step:
        image: {{name: '{TARGET}:v3.1@{digest}', username: $USER, password: $PASSWORD}}
    - step:
        image: {TARGET}@{digest}
    - step: {{image: {TARGET}}}
    - step: {{image: {TARGET}-sibling:v3.1}}
    - step: {{image: other.example/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base:v3.1}}
"""
        self.assertEqual(
            pipeline_images(text),
            ([
                (f"{TARGET}:v3.1", 1, "pipeline-global"),
                (f"{TARGET}:v3.1@{digest}", 5, "pipeline-step"),
                (f"{TARGET}@{digest}", 7, "pipeline-step"),
                (TARGET, 8, "pipeline-step"),
            ], []),
        )

    def test_reachable_aliases_merges_parallel_stages_and_services(self):
        text = f"""definitions:
  steps:
    - step: &compile
        image: &build_image
          name: {TARGET}:build
          password: '${{{{PRIVATE_SECRET}}}}'
        services: [postgres, docker]
    - step: &unused
        image: {TARGET}:unused
  services:
    postgres:
      image: {{<<: *build_image, name: '{TARGET}:postgres'}}
    never-used:
      image: {TARGET}:unused-service
pipelines:
  custom:
    release:
      - variables:
          - name: image
            default: {TARGET}:not-a-step
      - stage:
          steps:
            - step: *compile
            - parallel:
                steps:
                  - step: {{<<: *compile, image: 'alpine:3'}}
                  - step:
                      script: ['docker pull {TARGET}:script-only']
                      image:
                        <<: *build_image
  branches:
    main:
      - parallel:
          - step: *compile
"""
        self.assertEqual(
            pipeline_images(text),
            ([
                (f"{TARGET}:build", 5, "pipeline-step"),
                (f"{TARGET}:postgres", 12, "pipeline-service"),
            ], []),
        )

    def test_merge_queue_image_declaration(self):
        text = f"""pipelines:
  merge-queues:
    merge_release:
      - step: {{image: '{TARGET}:merge-queue'}}
"""
        self.assertEqual(pipeline_images(text), ([(f"{TARGET}:merge-queue", 4, "pipeline-step")], []))

    def test_yaml_merge_sequence_earlier_source_and_explicit_field_win(self):
        text = f"""one: &one {{image: '{TARGET}:one'}}
other: &other {{image: '{TARGET}:other'}}
image:
  <<: [{{name: alpine}}, {{name: '{TARGET}:not-used'}}]
pipelines:
  default:
    - step: {{<<: [*one, *other]}}
    - step: {{image: alpine, <<: [*one, *other]}}
"""
        self.assertEqual(pipeline_images(text), ([(f"{TARGET}:one", 1, "pipeline-step")], []))

    def test_dynamic_images_only_report_possible_target_and_redact(self):
        text = f"""image: '{TARGET}:${{{{SUPER_SECRET}}}}'
ignored: []
pipelines:
  default:
    - step: {{image: '${{PRIVATE_IMAGE}}'}}
    - step: {{image: 'registry.other.example/other:${{PRIVATE_TAG}}'}}
    - step: {{image: '${{PRIVATE_HOST}}/different/image:${{PRIVATE_TAG}}'}}
    - step: {{image: '{TARGET}-other:${{PRIVATE_TAG}}'}}
    - step: {{image: '${{PRIVATE_HOST}}/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base:fixed'}}
    - step: {{image: '{TARGET}${{VERSION_SUFFIX}}'}}
"""
        records, errors = pipeline_images(text)
        self.assertEqual(records, [])
        self.assertEqual([error.split(":")[0] for error in errors], ["line 1", "line 5", "line 9", "line 10"])
        self.assertNotIn("PRIVATE", " ".join(errors))
        self.assertNotIn("SUPER_SECRET", " ".join(errors))

    def test_dynamic_used_service_but_not_an_unused_definition(self):
        text = """definitions:
  services:
    used: {image: '${SERVICE_IMAGE}'}
    unused: {image: '${OTHER_SERVICE_IMAGE}'}
pipelines:
  default:
    - step: {services: [used]}
"""
        self.assertEqual(pipeline_images(text), ([], ["line 3: unresolved pipeline-service image reference"]))

    def test_dynamic_service_selection_must_be_able_to_reach_target(self):
        text = f"""definitions:
  services:
    app-build: {{image: '{TARGET}:service'}}
    database-mysql: {{image: 'mysql:${{VERSION}}'}}
pipelines:
  default:
    - step: {{services: ['database-${{DATABASE}}']}}
    - step: {{services: ['app-${{APP}}']}}
"""
        self.assertEqual(pipeline_images(text), ([], ["line 8: unresolved pipeline service selection"]))

    def test_yaml_errors_are_redacted_and_other_steps_are_preserved(self):
        self.assertEqual(pipeline_images("image: [DO_NOT_LOG\n")[0], [])
        self.assertNotIn("DO_NOT_LOG", str(pipeline_images("image: [DO_NOT_LOG\n")))
        self.assertTrue(pipeline_images("image: [DO_NOT_LOG\n")[1])
        records, errors = pipeline_images(f"""image: {{password: DO_NOT_LOG}}
pipelines:
  default:
    - step: {{image: '{TARGET}:ok'}}
    - parallel: &loop [{{parallel: *loop}}]
""")
        self.assertEqual(records, [(f"{TARGET}:ok", 4, "pipeline-step")])
        self.assertEqual(len(errors), 2)
        self.assertNotIn("DO_NOT_LOG", str(errors))


class DockerImageTests(unittest.TestCase):
    def test_legacy_full_repo_and_id_from_first_historical_format(self):
        text = f"""#!/bin/bash
_docker_image_repo='{TARGET}'
_docker_image_tag="v3.11.1"
_docker_image_commit='fbfd91a5'
_docker_image_id="${{_docker_image_tag}}-${{_docker_image_commit}}"
# docker pull {TARGET}:v9.9-wrong
DOCKER_IMAGE=$_docker_image_repo:"$_docker_image_id" # actual consumer
export DOCKER_IMAGE CONTAINER_NAME
"""
        self.assertEqual(docker_images(text), ([(f"{TARGET}:v3.11.1-fbfd91a5", 7, "docker-config")], []))

    def test_legacy_separate_registry_folder_and_repo(self):
        registry, repo = TARGET.rsplit("/", 1)
        text = f"""_docker_hub_registry="{registry}"
_docker_image_repo={repo}
_docker_image_tag=v4.2
_docker_image_commit=836df811
export DOCKER_IMAGE=$_docker_hub_registry/$_docker_image_repo:"$_docker_image_tag-$_docker_image_commit"
"""
        self.assertEqual(docker_images(text), ([(f"{TARGET}:v4.2-836df811", 5, "docker-config")], []))

    def test_registry_case_and_explicit_acr(self):
        text = """FLEXIV_DOCKER_REGISTRY="${FLEXIV_DOCKER_REGISTRY:-hub}"
case "$FLEXIV_DOCKER_REGISTRY" in
  hub) _docker_registry_host="hub.flexiv.cloud" ;;
  acr) _docker_registry_host="hub-acr.flexiv.cloud" ;;
  *)
    FLEXIV_DOCKER_REGISTRY="hub"
    _docker_registry_host="hub.flexiv.cloud"
    ;;
esac
_docker_hub_registry="$_docker_registry_host/flexiv_sw_env"
_docker_image_repo=flexiv_thirdparty2_for_flexiv_sw_base
DOCKER_IMAGE="$_docker_hub_registry/$_docker_image_repo:main-12345678"
"""
        self.assertEqual(docker_images(text), ([(f"{TARGET}:main-12345678", 12, "docker-config")], []))
        self.assertEqual(docker_images('FLEXIV_DOCKER_REGISTRY=acr\n' + text), ([], []))

    def test_registry_if_ignores_inactive_acr_rewrite(self):
        text = f"""DOCKER_IMAGE='{TARGET}:original'
if [[ "$FLEXIV_DOCKER_REGISTRY" == "acr" ]]; then
    DOCKER_IMAGE="hub-acr.flexiv.cloud/flexiv_sw_env/flexiv_thirdparty2_for_flexiv_sw_base:original"
fi
"""
        self.assertEqual(docker_images(text), ([(f"{TARGET}:original", 1, "docker-config")], []))

    def test_modern_awk_reader_is_not_an_independent_reference(self):
        text = dedent('''\
            _pipeline_file="$_project_path/bitbucket-pipelines.yml"
            if ! DOCKER_IMAGE=$(awk '
                /^image:[[:space:]]*(#.*)?$/ { in_image = 1; next }
                /^[^[:space:]#]/ { in_image = 0 }
                in_image && /^[[:space:]]+name:[[:space:]]*/ {
                    sub(/^[[:space:]]+name:[[:space:]]*/, "")
                    sub(/[[:space:]]+#.*$/, "")
                    sub(/[[:space:]]+$/, "")
                    print
                    exit
                }
            ' "$_pipeline_file"); then
                return 1
            fi
            if [[ "$DOCKER_IMAGE" == \\"*\\" || "$DOCKER_IMAGE" == \\'*\\' ]]; then
                DOCKER_IMAGE="${DOCKER_IMAGE:1:${#DOCKER_IMAGE}-2}"
            fi
            if [[ "$FLEXIV_DOCKER_REGISTRY" == "acr" ]]; then
                DOCKER_IMAGE="$_docker_hub_registry/$_docker_image_repo:$_docker_image_tag-$_docker_image_commit"
            fi
        ''')
        self.assertEqual(docker_images(text), ([], []))
        self.assertTrue(docker_images(text.replace("print", "launch_unrecognized_command"))[1])

    def test_do_not_execute_shell_or_expose_unresolved_values(self):
        records, errors = docker_images(f'DOCKER_IMAGE="{TARGET}:$(printf DO_NOT_LOG)"')
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)
        self.assertNotIn("DO_NOT_LOG", str(errors))
        self.assertEqual(docker_images('DOCKER_IMAGE="unrelated.example/image:$DO_NOT_LOG"'), ([], []))
        self.assertEqual(docker_images("# comment\n"), ([], []))
        self.assertTrue(docker_images("echo DO_NOT_LOG")[1])
        self.assertNotIn("DO_NOT_LOG", str(docker_images("echo DO_NOT_LOG")))

    def test_functions_heredocs_and_comments_are_not_consumers(self):
        text = f"""DOCKER_IMAGE=python:3.11
producer() {{
    DOCKER_IMAGE='{TARGET}:function'
}}
cat <<'EOF'
DOCKER_IMAGE='{TARGET}:heredoc'
EOF
# DOCKER_IMAGE={TARGET}:comment
"""
        self.assertEqual(docker_images(text), ([], []))


if __name__ == "__main__":
    unittest.main()
