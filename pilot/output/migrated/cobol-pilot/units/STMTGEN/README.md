# stmtgen

This Spring Batch module translates COBOL program STMTGEN into a batch job that opens a customer master input dataset, reads each customer, executes SQL to total period transactions, calculates interest through the migrated INTCALC-compatible service API, writes a daily statement output dataset, and inserts statement history rows into the database.

## Run

