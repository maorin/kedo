"""CLI entry point for kedo.

Usage examples::

    # Run the full pipeline (interactive review)
    kedo run "Build a REST API that manages a to-do list"

    # Run with auto-approval and a custom output directory
    kedo run --auto-approve --output-dir ./my-project "..."

    # Show help
    kedo --help
    kedo run --help
"""

from __future__ import annotations

import sys
from typing import Optional

import click

from kedo import __version__
from kedo.config import load_config
from kedo.pipeline import Pipeline
from kedo.stages import (
    AnalysisStage,
    CodegenStage,
    DecompositionStage,
    DeploymentStage,
    EvaluationStage,
    ReviewStage,
    TestingStage,
)


def _build_pipeline(config: dict) -> Pipeline:
    """Instantiate and return the default pipeline."""
    stages = [
        AnalysisStage(config),
        DecompositionStage(config),
        CodegenStage(config),
        TestingStage(config),
        EvaluationStage(config),
        ReviewStage(config),
        DeploymentStage(config),
    ]
    try:
        from rich.console import Console  # type: ignore

        console = Console()
    except ImportError:
        console = None
    return Pipeline(stages=stages, console=console)


@click.group()
@click.version_option(__version__, prog_name="kedo")
def main() -> None:
    """kedo — 从需求到部署的全流程自动化开发工具。"""


@main.command()
@click.argument("requirement")
@click.option(
    "--config",
    "config_path",
    default=None,
    metavar="FILE",
    help="Path to a kedo.yaml configuration file.",
)
@click.option(
    "--model",
    default=None,
    help="LLM model to use (overrides config file).",
)
@click.option(
    "--output-dir",
    default=None,
    metavar="DIR",
    help="Directory to write generated files into.",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Skip the human-review step and auto-approve.",
)
@click.option(
    "--no-manifest",
    is_flag=True,
    default=False,
    help="Do not generate a deployment manifest.",
)
def run(
    requirement: str,
    config_path: Optional[str],
    model: Optional[str],
    output_dir: Optional[str],
    auto_approve: bool,
    no_manifest: bool,
) -> None:
    """Run the full development pipeline for REQUIREMENT.

    REQUIREMENT is a natural-language description of what you want to build.
    """
    config = load_config(config_path)

    # CLI flags override config-file values
    if model:
        config["model"] = model
    if output_dir:
        config["output_dir"] = output_dir
    if auto_approve:
        config["auto_approve"] = True
    if no_manifest:
        config["generate_manifest"] = False

    pipeline = _build_pipeline(config)
    try:
        context = pipeline.run(requirement)
    except RuntimeError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Exit with non-zero status if the pipeline did not reach deployment
    results = context.get("_results", [])
    if any(r.failed for r in results):
        sys.exit(1)
