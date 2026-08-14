"""Dynamic DAG generation: one DAG per active `Pipeline`, read from
`apps.orchestration_api` at every DAG-file parse cycle rather than
hand-written per pipeline -- see
docs/decisions/0010-airflow-orchestration.md for the full design.

This file runs inside Airflow's own container/Python environment (see
`x-airflow-common` in docker-compose.yml) and never imports Django or
any of this project's own code -- the only contact with the platform is
plain HTTP against the three `apps.orchestration_api` endpoints, using
`requests` (already a core Airflow dependency, no extra provider
package needed).

Each generated DAG has two real tasks with a real dependency, not one
opaque "run everything" task:

  trigger_run  ->  wait_for_completion

`trigger_run` calls the trigger endpoint (which wraps the exact same
`apps.execution.dispatch.trigger_manual_run` the web UI and
`manage.py run_pipeline` call) and pushes the new run id to XCom.
`wait_for_completion` is a classic Airflow *sensor* (`PythonSensor`,
`mode="reschedule"` so it releases its worker slot between pokes instead
of blocking one for the run's whole duration) polling the status
endpoint until the run reaches a terminal state, raising on `FAILED` so
Airflow's own retry/alerting layer sees it -- on top of, not instead of,
`apps.execution.retry_policy`'s own retries for a single run's transient
failures. See ADR 0010 for why both layers exist and what each is for.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import requests
from airflow import DAG
from airflow.exceptions import AirflowException
from airflow.operators.python import PythonOperator
from airflow.sensors.python import PythonSensor

API_BASE_URL = os.environ.get("ORCHESTRATION_API_BASE_URL", "http://web:8000/internal/api")
API_TOKEN = os.environ.get("ORCHESTRATION_API_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = 30

DEFAULT_START_DATE = datetime(2024, 1, 1)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _fetch_active_pipelines() -> list[dict[str, Any]]:
    """Never let a transient failure to reach the platform's web service
    crash DAG-file parsing (which would take every other DAG down with
    it) -- an empty list here just means "generate no DAGs this parse
    cycle," and the scheduler will simply try again on the next one."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/pipelines/", headers=_auth_headers(), timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json()["pipelines"]
    except requests.RequestException:
        return []


def _trigger_run(schema_name: str, pipeline_id: str, **_context) -> str:
    response = requests.post(
        f"{API_BASE_URL}/pipelines/{pipeline_id}/trigger/",
        json={"schema_name": schema_name},
        headers=_auth_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["run_id"]  # PythonOperator's return value -> XCom


def _run_is_terminal(schema_name: str, **context) -> bool:
    run_id = context["ti"].xcom_pull(task_ids="trigger_run")
    response = requests.get(
        f"{API_BASE_URL}/runs/{run_id}/status/",
        params={"schema_name": schema_name},
        headers=_auth_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()

    if body["status"] == "failed":
        raise AirflowException(
            f"pipeline run {run_id} failed "
            f"(category={body.get('error_category')!r}, "
            f"retryable={body.get('error_is_retryable')})"
        )
    return bool(body["is_terminal"])


def _build_dag(pipeline: dict[str, Any]) -> DAG:
    dag_id = f"pipeline_{pipeline['pipeline_id']}"
    with DAG(
        dag_id=dag_id,
        description=f"{pipeline['name']} ({pipeline['schema_name']})",
        schedule=pipeline["schedule_cron"] or None,
        start_date=DEFAULT_START_DATE,
        catchup=False,
        max_active_runs=1,
        tags=["datalake", pipeline["schema_name"]],
        default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    ) as dag:
        trigger_run = PythonOperator(
            task_id="trigger_run",
            python_callable=_trigger_run,
            op_kwargs={
                "schema_name": pipeline["schema_name"],
                "pipeline_id": pipeline["pipeline_id"],
            },
        )
        wait_for_completion = PythonSensor(
            task_id="wait_for_completion",
            python_callable=_run_is_terminal,
            op_kwargs={"schema_name": pipeline["schema_name"]},
            poke_interval=15,
            timeout=60 * 60,
            mode="reschedule",
        )
        trigger_run >> wait_for_completion
    return dag


for _pipeline in _fetch_active_pipelines():
    _dag = _build_dag(_pipeline)
    globals()[_dag.dag_id] = _dag
