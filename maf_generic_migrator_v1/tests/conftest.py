"""Test fixtures for platform + cartridge tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tiny_polyglot_repo(tmp_path: Path) -> Path:
    """Create a minimal polyglot multi-Lambda AWS repo for smoke tests."""

    py_lambda = tmp_path / "order_processor"
    py_lambda.mkdir()
    (py_lambda / "handler.py").write_text(
        "import boto3\n"
        "import os\n"
        "\n"
        "sqs = boto3.client('sqs')\n"
        "ddb = boto3.resource('dynamodb')\n"
        "\n"
        "def lambda_handler(event, context):\n"
        "    table = ddb.Table(os.environ['ORDERS_TABLE'])\n"
        "    for rec in event.get('Records', []):\n"
        "        table.put_item(Item=rec)\n"
        "        sqs.send_message(QueueUrl=os.environ['DS_QUEUE_URL'], MessageBody='enqueued')\n"
        "    return {'statusCode': 200}\n"
    )

    node_lambda = tmp_path / "notification_sender"
    node_lambda.mkdir()
    (node_lambda / "index.js").write_text(
        "const { SNSClient, PublishCommand } = require('@aws-sdk/client-sns');\n"
        "const sns = new SNSClient();\n"
        "exports.handler = async (event) => {\n"
        "  await sns.send(new PublishCommand({ TopicArn: process.env.TOPIC_ARN, Message: 'hi' }));\n"
        "};\n"
    )

    java_lambda = tmp_path / "invoice_generator"
    java_lambda.mkdir()
    (java_lambda / "pom.xml").write_text("<project/>")
    (java_lambda / "InvoiceHandler.java").write_text(
        "import com.amazonaws.services.lambda.runtime.Context;\n"
        "import com.amazonaws.services.lambda.runtime.RequestHandler;\n"
        "import software.amazon.awssdk.services.s3.S3Client;\n"
        "public class InvoiceHandler implements RequestHandler<Object, String> {\n"
        "    private final S3Client s3 = S3Client.builder().build();\n"
        "    public String handleRequest(Object input, Context ctx) { return \"ok\"; }\n"
        "}\n"
    )

    (tmp_path / "template.yaml").write_text(
        "AWSTemplateFormatVersion: '2010-09-09'\n"
        "Transform: AWS::Serverless-2016-10-31\n"
        "Resources:\n"
        "  OrderProcessor:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      CodeUri: order_processor/\n"
        "      Handler: handler.lambda_handler\n"
        "      Events:\n"
        "        OrdersQueue:\n"
        "          Type: SQS\n"
        "          Properties:\n"
        "            Queue: arn:aws:sqs:eu-west-1:123:orders\n"
        "  NotificationSender:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      CodeUri: notification_sender/\n"
        "      Handler: index.handler\n"
        "      Events:\n"
        "        FromDS:\n"
        "          Type: SQS\n"
        "          Properties:\n"
        "            Queue: arn:aws:sqs:eu-west-1:123:downstream\n"
        "  InvoiceGenerator:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      CodeUri: invoice_generator/\n"
        "      Handler: InvoiceHandler\n"
    )

    return tmp_path
