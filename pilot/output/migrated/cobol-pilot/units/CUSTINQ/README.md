# CUSTINQ

This module translates the CICS COBOL transaction `CUSTINQ` into a Spring Boot REST API that receives a customer inquiry request, looks up the customer in the database, and returns either the customer name for active accounts, an account suspended message for suspended accounts, or a customer not found / receive failed message based on the original CICS flow.

## Endpoints

### `POST /custinq`

Request body:
