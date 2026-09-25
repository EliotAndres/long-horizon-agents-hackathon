# Deploys the Tinybird project (datasource + condition endpoints) with the Tinybird Forward CLI.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir tinybird
WORKDIR /project
COPY datasources ./datasources
COPY endpoints ./endpoints
COPY deploy.sh ./
ENV TB_CLI_TELEMETRY_OPTOUT=1 TB_VERSION_WARNING=0
CMD ["sh", "deploy.sh"]
