import base64
import json
import functools
import collections
import pathlib
import urllib.parse
import dill
import pytest
from fastapi.testclient import TestClient
import sqlmodel
import sqlmodel.pool
import requests
from river import datasets, linear_model, preprocessing

from beaver.main import app
from beaver.db import engine, get_session
import beaver_sdk


@pytest.fixture(name="session")
def session_fixture():
    engine = sqlmodel.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlmodel.pool.StaticPool,
    )
    sqlmodel.SQLModel.metadata.create_all(engine)
    with sqlmodel.Session(engine) as session:
        yield session


@pytest.fixture()
def client(session: sqlmodel.Session):
    def get_session_override():
        yield session

    def request_override(*args, **kwargs):
        # This is a hack so that the Beaver SDK talks to the TestClient, instead of requests
        self, *args = args
        method, endpoint, *args = args
        endpoint = urllib.parse.urljoin(self.host, endpoint)
        r = client.request(method, endpoint, *args, **kwargs)
        r.raise_for_status()
        return r

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app)
    beaver_sdk.SDK.request = request_override
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def sdk():
    return beaver_sdk.Instance(host="")


@pytest.fixture()
def sqlite_mb_path():
    here = pathlib.Path(__file__).parent
    yield here / "message_bus.db"
    (here / "message_bus.db").unlink(missing_ok=True)


@pytest.fixture()
def create_message_bus(client: TestClient, sqlite_mb_path: pathlib.Path):

    # Create source
    response = client.post(
        "/api/message-bus",
        json={"name": "test_mb", "protocol": "SQLITE", "url": str(sqlite_mb_path)},
    )
    assert response.status_code == 201
    assert len(client.get("/api/message-bus/").json()) == 1
    assert client.get("/api/message-bus/test_mb").json()["protocol"] == "SQLITE"


@pytest.fixture()
def create_stream_processor(client: TestClient, sqlite_mb_path: pathlib.Path):

    # Create source
    response = client.post(
        "/api/stream-processor",
        json={"name": "test_sp", "protocol": "SQLITE", "url": str(sqlite_mb_path)},
    )
    assert response.status_code == 201
    assert len(client.get("/api/stream-processor/").json()) == 1
    assert client.get("/api/stream-processor/test_sp").json()["protocol"] == "SQLITE"


@pytest.fixture()
def create_job_runner(client: TestClient, sqlite_mb_path: pathlib.Path):

    # Create source
    response = client.post(
        "/api/task-runner",
        json={"name": "test_tr", "protocol": "SYNCHRONOUS"},
    )
    assert response.status_code == 201
    assert len(client.get("/api/task-runner/").json()) == 1
    assert client.get("/api/task-runner/test_tr").json()["protocol"] == "SYNCHRONOUS"


def test_phishing(
    create_message_bus, create_stream_processor, create_job_runner, client, sdk
):

    # Create a project
    project = sdk.project.create(
        name="phishing_project",
        task="BINARY_CLASSIFICATION",
        message_bus_name="test_mb",
        stream_processor_name="test_sp",
        job_runner_name="test_tr",
    )

    # Send 10 samples, without revealing answers
    message_bus = sdk.message_bus("test_mb")
    for i, (x, _) in enumerate(datasets.Phishing().take(10)):
        message_bus.send(topic="phishing_project_features", key=i, value=x)

    # Create a target
    project.define_target(
        query="SELECT key, created_at, value FROM messages WHERE topic = 'phishing_project_targets'",
        key_field="key",
        ts_field="created_at",
        value_field="value",
    )

    # Create a feature set
    response = client.post(
        "/api/feature-set",
        json={
            "name": "phishing_project_features",
            "project_name": "phishing_project",
            "query": "SELECT key, created_at, value FROM messages WHERE topic = 'phishing_project_features'",
            "key_field": "key",
            "ts_field": "created_at",
            "value_field": "value",
        },
    )
    assert response.status_code == 201

    # Create an experiment
    model = preprocessing.StandardScaler() | linear_model.LogisticRegression()
    model.learn = model.learn_one
    model.predict = model.predict_one
    response = client.post(
        "/api/experiment",
        json={
            "name": "phishing_experiment_1",
            "project_name": "phishing_project",
            "feature_set_name": "phishing_project_features",
            "model": base64.b64encode(dill.dumps(model)).decode("ascii"),
            "start_from_top": True,
        },
    )
    assert response.status_code == 201

    # Create a second experiment
    model = preprocessing.StandardScaler() | linear_model.Perceptron()
    model.learn = model.learn_one
    model.predict = model.predict_one
    response = client.post(
        "/api/experiment",
        json={
            "name": "phishing_experiment_2",
            "project_name": "phishing_project",
            "feature_set_name": "phishing_project_features",
            "model": base64.b64encode(dill.dumps(model)).decode("ascii"),
            "start_from_top": True,
        },
    )
    assert response.status_code == 201

    # Check predictions were made
    response = client.get("/api/project/phishing_project").json()
    assert response["experiments"]["phishing_experiment_1"]["n_predictions"] == 10
    assert response["experiments"]["phishing_experiment_1"]["n_learnings"] == 0
    assert response["experiments"]["phishing_experiment_1"]["accuracy"] == 0
    assert response["experiments"]["phishing_experiment_2"]["n_predictions"] == 10
    assert response["experiments"]["phishing_experiment_2"]["n_learnings"] == 0
    assert response["experiments"]["phishing_experiment_2"]["accuracy"] == 0

    # The first 10 samples were sent without labels -- send them in now
    for i, (_, y) in enumerate(datasets.Phishing().take(10)):
        assert (
            client.post(
                "/api/message-bus/test_mb",
                json={
                    "topic": "phishing_project_targets",
                    "key": str(i),
                    "value": json.dumps(y),
                },
            ).status_code
            == 201
        )

    # We're using a synchronous task runner. Therefore, even though the labels have been sent, the
    # experiments are not automatically picking them up for learning. We have to explicitely make
    # them learn.
    response = client.put("/api/experiment/phishing_experiment_1/start")
    assert response.status_code == 200
    response = client.put("/api/experiment/phishing_experiment_2/start")
    assert response.status_code == 200

    # Check learning happened
    response = client.get("/api/project/phishing_project").json()
    assert response["experiments"]["phishing_experiment_1"]["n_predictions"] == 10
    assert response["experiments"]["phishing_experiment_1"]["n_learnings"] == 10
    assert response["experiments"]["phishing_experiment_1"]["accuracy"] == 0.3
    assert response["experiments"]["phishing_experiment_2"]["n_predictions"] == 10
    assert response["experiments"]["phishing_experiment_2"]["n_learnings"] == 10
    assert response["experiments"]["phishing_experiment_2"]["accuracy"] == 0.3

    # Send next 5 samples, with labels
    for i, (x, y) in enumerate(datasets.Phishing().take(15)):
        if i < 10:
            continue
        assert (
            client.post(
                "/api/message-bus/test_mb",
                json={
                    "topic": "phishing_project_features",
                    "key": str(i),
                    "value": json.dumps(x),
                },
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/message-bus/test_mb",
                json={
                    "topic": "phishing_project_targets",
                    "key": str(i),
                    "value": json.dumps(y),
                },
            ).status_code
            == 201
        )

    # Run the models
    response = client.put("/api/experiment/phishing_experiment_1/start")
    assert response.status_code == 200
    response = client.put("/api/experiment/phishing_experiment_2/start")
    assert response.status_code == 200

    # Check stats
    response = client.get("/api/project/phishing_project").json()
    assert response["experiments"]["phishing_experiment_1"]["n_predictions"] == 15
    assert response["experiments"]["phishing_experiment_1"]["n_learnings"] == 15
    assert (
        round(response["experiments"]["phishing_experiment_1"]["accuracy"], 3) == 0.467
    )
    assert response["experiments"]["phishing_experiment_2"]["n_predictions"] == 15
    assert response["experiments"]["phishing_experiment_2"]["n_learnings"] == 15
    assert (
        round(response["experiments"]["phishing_experiment_2"]["accuracy"], 3) == 0.533
    )
