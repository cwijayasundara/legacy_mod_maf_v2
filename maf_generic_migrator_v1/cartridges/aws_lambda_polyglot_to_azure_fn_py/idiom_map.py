"""Handler-shape and idiom rewrites: AWS Lambda handler -> Azure Functions handler (Python)."""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.cartridge import IdiomMapping

IDIOM_MAP: list[IdiomMapping] = [
    IdiomMapping(
        name="python_handler_shape",
        source_pattern="def lambda_handler(event, context):",
        target_pattern="@app.route(route='...') / @app.service_bus_queue_trigger(...)\ndef handler(req_or_msg: ...) -> ...:",
        explanation="Lambda handler takes (event, context); Azure Functions uses typed bindings via decorators.",
    ),
    IdiomMapping(
        name="node_handler_shape",
        source_pattern="exports.handler = async (event, context) => { ... }",
        target_pattern="Python Azure Function with typed binding decorator",
        explanation="Node handler must be rewritten to idiomatic Python with Azure Functions v2 binding model.",
    ),
    IdiomMapping(
        name="java_handler_shape",
        source_pattern="public class MyHandler implements RequestHandler<In, Out> { public Out handleRequest(In in, Context ctx) }",
        target_pattern="Python Azure Function. Inputs/outputs become Pydantic models.",
        explanation="Statically-typed Java handler becomes dynamically-typed Python handler with explicit schemas.",
    ),
    IdiomMapping(
        name="cs_handler_shape",
        source_pattern="public class Function { public Out FunctionHandler(In in, ILambdaContext ctx) }",
        target_pattern="Python Azure Function with Pydantic models.",
        explanation="Amazon.Lambda.Core handler becomes Python Azure Function.",
    ),
    IdiomMapping(
        name="boto3_to_azure_sdk",
        source_pattern="boto3.client('SVC').OPERATION(...)",
        target_pattern="azure.<service>.Client(...).operation(...)",
        explanation="AWS SDK call is replaced by equivalent Azure SDK call; service-map drives specifics.",
    ),
    IdiomMapping(
        name="iam_to_managed_identity",
        source_pattern="role: arn:aws:iam::...:role/LambdaRole",
        target_pattern="DefaultAzureCredential() with Managed Identity",
        explanation="No role assumption in code; Azure SDK picks up credential from environment.",
    ),
    IdiomMapping(
        name="env_vars",
        source_pattern="Lambda env vars (template.yaml / serverless.yml)",
        target_pattern="Azure Functions app settings (local.settings.json / Bicep appSettings)",
        explanation="Name-for-name port; sensitive values reference Key Vault secrets.",
    ),
    IdiomMapping(
        name="stepfunctions_to_durable",
        source_pattern="AWS Step Functions state-machine ASL",
        target_pattern="Azure Durable Functions orchestrator + activity functions",
        explanation="Each state becomes an activity; orchestrator composes them in Python.",
    ),
]
