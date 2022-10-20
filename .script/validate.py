"""
Dry run generated queries.

Passes all queries defined under sql/ to a Cloud Function that will run the
queries with the dry_run option enabled.

We could provision BigQuery credentials to the CircleCI job to allow it to run
the queries directly, but there is no way to restrict permissions such that
only dry runs can be performed. In order to reduce risk of CI or local users
accidentally running queries during tests, leaking and overwriting production
data, we proxy the queries through the dry run service endpoint.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

import click
import requests
import requests.exceptions
from metric_config_parser.config import (
    DEFINITIONS_DIR,
    ConfigCollection,
    entity_from_path,
)
from metric_config_parser.function import FunctionsSpec


logger = logging.getLogger(__name__)


DRY_RUN_URL = (
    "https://us-central1-moz-fx-data-shared-prod.cloudfunctions.net/"
    "bigquery-etl-dryrun"
)
FUNCTION_CONFIG = "functions.toml"
TEMPLATES_DIR = Path(__file__).parent / "templates"


@click.group()
def cli():
    """Initialize CLI."""
    pass


class DryRunFailedError(Exception):
    """Exception raised when dry run fails."""

    def __init__(self, error: Any, sql: str):
        """Initialize exception."""
        self.sql = sql
        super().__init__(error)


def dry_run_query(sql: str) -> None:
    """Dry run the provided SQL query."""
    try:
        r = requests.post(
            DRY_RUN_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps({"dataset": "mozanalysis", "query": sql}).encode("utf8"),
        )
        response = r.json()
    except Exception as e:
        # This may be a JSONDecode exception or something else.
        # If we got a HTTP exception, that's probably the most interesting thing to raise.
        try:
            r.raise_for_status()
        except requests.exceptions.RequestException as request_exception:
            e = request_exception
        raise DryRunFailedError(e, sql)

    if response["valid"]:
        logger.info("Dry run OK")
        return

    if "errors" in response and len(response["errors"]) == 1:
        error = response["errors"][0]
    else:
        error = None

    if (
        error
        and error.get("code", None) in [400, 403]
        and "does not have bigquery.tables.create permission for dataset"
        in error.get("message", "")
    ):
        # We want the dryrun service to only have read permissions, so
        # we expect CREATE VIEW and CREATE TABLE to throw specific
        # exceptions.
        logger.info("Dry run OK")
        return

    raise DryRunFailedError(
        (error and error.get("message", None)) or response["errors"], sql=sql
    )


@cli.command("validate")
@click.argument("path", type=click.Path(exists=True), nargs=-1)
@click.option(
    "--config_repos",
    "--config-repos",
    help="URLs to public repos with configs",
    multiple=True,
)
def validate(path, config_repos):
    """Validate config files."""
    dirty = False
    config_collection = ConfigCollection.from_github_repos(config_repos)

    # get updated function definitions
    for config_file in path:
        config_file = Path(config_file)
        if config_file.is_file() and config_file.name == FUNCTION_CONFIG:
            functions = entity_from_path(config_file)
            config_collection.functions = functions

    # get updated definition files
    for config_file in path:
        config_file = Path(config_file)
        if not config_file.is_file():
            continue
        if ".example" in config_file.suffixes:
            print(f"Skipping example config {config_file}")
            continue

        if config_file.parent.name == DEFINITIONS_DIR:
            entity = entity_from_path(config_file)
            try:
                if not isinstance(entity, FunctionsSpec):
                    entity.validate(config_collection, None)
            except Exception as e:
                dirty = True
                print(e)
            else:
                if not isinstance(entity, FunctionsSpec):
                    try:
                        validation_template = (
                            Path(TEMPLATES_DIR) / "validation_query.sql"
                        ).read_text()

                        for (
                            metric_name,
                            metric,
                        ) in entity.spec.metrics.definitions.items():
                            entity.spec.metrics.definitions[
                                metric_name
                            ].select_expression = (
                                config_collection.get_env()
                                .from_string(metric.select_expression)
                                .render()
                            )

                        env = config_collection.get_env().from_string(
                            validation_template
                        )

                        i = 0
                        progress = 0
                        metrics = []
                        for metric in entity.spec.metrics.definitions.values():
                            i += 1
                            metrics.append(metric)

                            if i % 10 == 0:
                                print(
                                    f"Dry running {config_file} ({progress}/{len(entity.spec.metrics.definitions.values()) % 10})"
                                )
                                sql = env.render(
                                    metrics=metrics,
                                    dimensions=[],
                                    segments=[],
                                    data_sources={
                                        name: d.resolve(None)
                                        for name, d in entity.spec.data_sources.definitions.items()
                                    },
                                )
                                dry_run_query(sql)
                                i = 0
                                metrics = []
                                progress += 1

                        sql = env.render(
                            metrics=metrics,
                            dimensions=entity.spec.dimensions.definitions.values(),
                            segments=entity.spec.segments.definitions.values(),
                            data_sources={
                                name: d.resolve(None)
                                for name, d in entity.spec.data_sources.definitions.items()
                            },
                        )
                        dry_run_query(sql)
                    except DryRunFailedError as e:
                        print("Error evaluating SQL:")
                        for i, line in enumerate(e.sql.split("\n")):
                            print(f"{i+1: 4d} {line.rstrip()}")
                        print("")
                        print(str(e))
                        dirty = True
                    else:
                        print(f"{config_file} OK")

    sys.exit(1 if dirty else 0)


if __name__ == "__main__":
    validate()