"""Names of the MCP tools the service exposes; tokens may only list these."""

TOOL_NAMES = frozenset({
    "health",
    "upload_dataset",
    "list_datasets",
    "inspect_dataset",
    "delete_dataset",
    # Phase 5: the plan engine (validates and compiles; nothing is executed yet)
    "validate_table_plan",
    "validate_plan",
    "compile_plan",
    "list_pipelines",
    "get_pipeline",
    "delete_pipeline",
    "get_plan_format",
    "list_saved_plans",
    "instantiate_saved_plan",
    # Phase 6: execution (sandboxed) and exports
    "run_pipeline",
    "get_run",
    "export_asset",
    "list_exports",
    "download_export",
    "delete_export",
})
