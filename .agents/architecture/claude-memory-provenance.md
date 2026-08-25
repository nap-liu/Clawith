# Claude project-memory consolidation provenance

This repository previously depended on personal Claude project memory for architecture and engineering rules. That made behavior agent-specific and left `AGENTS.md` pointing at missing files. The durable material was reviewed and consolidated on 2026-08-24.

Personal-memory content was not copied verbatim when it contained credentials, host addresses, personal filesystem paths, transient production versions, stale line numbers, historical test counts, or incident-only details. Stable architecture and lessons were retained below.

## Consolidated sources

| Destination | Claude memory sources consolidated |
|---|---|
| `ARCHITECTURE_SPEC_EN.md` | `local_dev_stack.md`, `web_turn_connection_decoupling.md`, `a2a_unified_loop_and_new_conversation.md`, `session_introspection_tools.md`, `cli_tools_two_execution_models.md`, `builtin_tool_schema_source_of_truth.md` |
| `conversations-and-turns.md` | `a2a_message_agent_id_normalized.md`, `cross_channel_web_live_update_writable_gate.md`, `im_channel_llm_timeout_fix.md`, `send_channel_file_channel_sender_gap.md`, `turn_recovery_transaction_timestamp_ordering.md`, `feedback_p2p_vs_group_semantic.md`, `deploy_interrupts_conversations_next_iteration.md` |
| `tools-and-sandboxes.md` | `builtin_tool_schema_source_of_truth.md`, `builtin_tool_seed_field_length_limits.md`, `tool_enablement_explicit_deploy.md`, `cli_tools_two_execution_models.md`, `cli_tools_independent_function_surface.md`, `feedback_platform_code_name_agnostic.md`, `feedback_fanout_not_gradual.md`, `sanitize_poisons_llm_history.md`, `repetitive_toolcall_guard.md` |
| `environments-and-operations.md` | `local_dev_stack.md`, `worktree_docker_test_and_browser_e2e.md`, `backend_test_recipe_local.md`, `fresh_db_local_stack_gotchas.md`, `worktree_e2e_shared_devdb_pitfalls.md`, `prod_deploy_layout.md`, `prod_compose_layout.md`, `prod_nginx_source_is_baked_frontend_template.md` |
| `.agents/rules/design_and_dev.md` | `feedback_modular_first.md`, `feedback_no_source_shape_tests.md`, `feedback_format_scope.md`, `feedback_quality_over_speed.md`, `feedback_evidence_based_reports.md`, `feedback_dont_bandaid_prompts_fix_source.md`, `feedback_no_inductive_negative_example_prompts.md`, `feedback_plan_before_apply.md` |
| `.agents/rules/deploy.md` | `feedback_testing_in_docker.md`, `feedback_verify_local_docker_not_prod.md`, `feedback_local_verify_before_prod.md`, `feedback_check_other_worktrees.md`, `feedback_backup_at_cutover_not_prep.md` |
| `.agents/rules/github.md` | `project_paths_and_prod.md`, `feedback_worktree_default.md`, `feedback_scope_company_main.md`, `feedback_specs_not_in_repo.md` |
| `.agents/rules/release.md` | `feedback_build_all_images_together.md`, `feedback_build_for_prod_platform.md`, `feedback_tag_specific_sha.md`, `feedback_no_private_version_bump.md`, `feedback_backup_at_cutover_not_prep.md` |
| `.agents/runbooks/production_release.md` | `prod_deploy_layout.md`, `prod_compose_layout.md`, `project_paths_and_prod.md`, `feedback_plan_before_apply.md`, `feedback_local_verify_before_prod.md`, `feedback_build_all_images_together.md`, `feedback_build_for_prod_platform.md`, `feedback_tag_specific_sha.md`, `feedback_no_private_version_bump.md`, `feedback_backup_at_cutover_not_prep.md`, `local_image_build_pitfalls_2026_07_02.md`, `prod_nginx_source_is_baked_frontend_template.md`, `prod_v1_10_1_deploy_and_api_upstream_gotcha.md`, `deploy_interrupts_conversations_next_iteration.md`, and durable release/rollback lessons from date-stamped production records |

## Intentionally not promoted to architecture

Date-stamped production release notes, one-off data corrections, current fleet inventories, usage-report outputs, current image tags, personal backup locations, host access instructions, and credentials remain operational history rather than repository architecture. Their durable release lessons are consolidated into the production runbook; sensitive or time-varying facts remain in the approved operations inventory. Examples include `*_prod_*.md`, `prod_usage_report_method.md`, `prod_server_mem_specs.md`, `backup_location.md`, `prod_access_via_clawith_ssh.md`, and `utm_x86_vm_local_validation_env.md`.

Feature-specific memories not listed above remain discoverable through Git history and current code. If a feature establishes a new durable invariant, update the appropriate architecture document and this provenance map in the same commit rather than adding another hidden personal-memory dependency.
