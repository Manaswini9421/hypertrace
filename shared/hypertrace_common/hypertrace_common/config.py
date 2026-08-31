from pydantic_settings import BaseSettings, SettingsConfigDict


class RabbitMQSettings(BaseSettings):
    """Connection settings for the shared `hypertrace.events` exchange.

    Defaults point at the in-cluster Service names from
    infra/k8s/rabbitmq/rabbitmq.yaml so services need no config in-cluster;
    override via RABBITMQ_* env vars for local/dev use.
    """

    model_config = SettingsConfigDict(env_prefix="RABBITMQ_")

    host: str = "rabbitmq.hypertrace.svc.cluster.local"
    port: int = 5672
    user: str = "hypertrace"
    password: str = "hypertrace-dev"
    vhost: str = "/"

    @property
    def url(self) -> str:
        return f"amqp://{self.user}:{self.password}@{self.host}:{self.port}{self.vhost}"


class DatabaseSettings(BaseSettings):
    """Connection settings for the TimescaleDB/Postgres instance.

    Defaults point at infra/k8s/postgres-timescaledb/timescaledb.yaml's
    in-cluster Service; override via DATABASE_* env vars for local/dev use.
    """

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    host: str = "timescaledb.hypertrace.svc.cluster.local"
    port: int = 5432
    user: str = "hypertrace"
    password: str = "hypertrace-dev"
    name: str = "hypertrace"

    @property
    def url(self) -> str:
        return f"postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
