# aws_polyglot — polyglot demo fixture

Three small AWS Lambdas wired via SAM:

| Lambda | Language | Trigger | Outbound |
| --- | --- | --- | --- |
| `order_processor` | Python | SQS `orders` | DynamoDB `orders` table + SQS `downstream` |
| `notification_sender` | Node.js | SQS `downstream` | SNS `notifications` topic |
| `invoice_generator` | Java | SQS `downstream` | S3 `invoices` bucket |

This fixture is the acceptance target for the `aws_lambda_polyglot_to_azure_fn_py`
cartridge. Running the platform against it should produce a `function_app.py`
per Lambda, translated to Azure Functions (Python v2 programming model).

Run offline (no LLM creds needed):

```bash
legacy-mod plan --cartridge aws_lambda_polyglot_to_azure_fn_py \
    --source examples/demo_migrations/aws_polyglot \
    --out .workspace/aws_polyglot_plan
```

Run live (requires `OPENAI_API_KEY` or `AZURE_OPENAI_ENDPOINT`):

```bash
python examples/run_live_migration.py
```
