"""Per-language complexity patterns for AWS Lambda sources.

Weights reflect observed difficulty of translation to Azure Functions (Python):
Step Functions / cross-service invocations / streaming are higher risk.
"""
from __future__ import annotations

PYTHON_PATTERNS: list[tuple[str, int, str]] = [
    (r"boto3\.client\(\s*['\"]stepfunctions['\"]", 4, "Step Functions"),
    (r"boto3\.client\(\s*['\"]eventbridge['\"]", 3, "EventBridge"),
    (r"boto3\.client\(\s*['\"]kinesis['\"]", 3, "Kinesis"),
    (r"boto3\.client\(\s*['\"]sqs['\"]", 2, "SQS"),
    (r"boto3\.client\(\s*['\"]sns['\"]", 2, "SNS"),
    (r"boto3\.client\(\s*['\"]dynamodb['\"]", 1, "DynamoDB"),
    (r"boto3\.client\(\s*['\"]s3['\"]", 1, "S3"),
    (r"(requests|urllib3?|httpx)\.(get|post|put|delete)", 2, "HTTP"),
    (r"import\s+(?:threading|multiprocessing|asyncio)", 2, "Concurrency"),
    (r"(PAGINATION|paginate|next_token|LastEvaluatedKey)", 2, "Pagination"),
]

NODE_PATTERNS: list[tuple[str, int, str]] = [
    (r"new\s+SFNClient", 4, "Step Functions"),
    (r"new\s+EventBridgeClient", 3, "EventBridge"),
    (r"new\s+KinesisClient", 3, "Kinesis"),
    (r"new\s+SQSClient", 2, "SQS"),
    (r"new\s+SNSClient", 2, "SNS"),
    (r"new\s+DynamoDBClient", 1, "DynamoDB"),
    (r"new\s+S3Client", 1, "S3"),
    (r"(axios|fetch|node-fetch|got)\(", 2, "HTTP"),
    (r"Promise\.all\(", 2, "Parallel async"),
]

JAVA_PATTERNS: list[tuple[str, int, str]] = [
    (r"\bSfnClient\b", 4, "Step Functions"),
    (r"\bEventBridgeClient\b", 3, "EventBridge"),
    (r"\bKinesisClient\b", 3, "Kinesis"),
    (r"\bSqsClient\b", 2, "SQS"),
    (r"\bSnsClient\b", 2, "SNS"),
    (r"\bDynamoDbClient\b", 1, "DynamoDB"),
    (r"\bS3Client\b", 1, "S3"),
    (r"CompletableFuture\.allOf", 2, "Parallel async"),
]

CSHARP_PATTERNS: list[tuple[str, int, str]] = [
    (r"\bAmazonStepFunctionsClient\b", 4, "Step Functions"),
    (r"\bAmazonEventBridgeClient\b", 3, "EventBridge"),
    (r"\bAmazonKinesisClient\b", 3, "Kinesis"),
    (r"\bAmazonSQSClient\b", 2, "SQS"),
    (r"\bAmazonSimpleNotificationServiceClient\b", 2, "SNS"),
    (r"\bAmazonDynamoDBClient\b", 1, "DynamoDB"),
    (r"\bAmazonS3Client\b", 1, "S3"),
    (r"Task\.WhenAll", 2, "Parallel async"),
]


def patterns_for(language: str) -> list[tuple[str, int, str]]:
    return {
        "python": PYTHON_PATTERNS,
        "node": NODE_PATTERNS,
        "typescript": NODE_PATTERNS,
        "java": JAVA_PATTERNS,
        "csharp": CSHARP_PATTERNS,
    }.get(language, [])
