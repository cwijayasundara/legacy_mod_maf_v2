"""Order processor Lambda.

Consumes messages from the `orders` SQS queue, persists each order to the
`orders` DynamoDB table, then enqueues a downstream notification message.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import boto3

_ddb = boto3.resource("dynamodb")
_sqs = boto3.client("sqs")


def lambda_handler(event, context):
    table = _ddb.Table(os.environ["ORDERS_TABLE"])
    downstream_url = os.environ["DOWNSTREAM_QUEUE_URL"]

    processed = 0
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        order_id = body.get("orderId") or str(uuid.uuid4())
        item = {
            "orderId": order_id,
            "customerId": body["customerId"],
            "amountCents": int(body["amountCents"]),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "status": "accepted",
        }
        table.put_item(Item=item)
        _sqs.send_message(
            QueueUrl=downstream_url,
            MessageBody=json.dumps({"orderId": order_id, "event": "order_accepted"}),
        )
        processed += 1

    return {"statusCode": 200, "body": json.dumps({"processed": processed})}
