       IDENTIFICATION DIVISION.
       PROGRAM-ID. STMTGEN.
      *   Monthly statement generator: reads customer master via VSAM,
      *   joins to DB2 transaction history, calls INTCALC for accrual,
      *   writes statement records.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT CUST-MASTER ASSIGN TO DDCUSTIN.
           SELECT STMT-OUT    ASSIGN TO DDSTMTOUT.
       DATA DIVISION.
       FILE SECTION.
       FD  CUST-MASTER.
       01  CUST-MASTER-REC.
           05  CMR-CUST-ID       PIC X(8).
           05  CMR-DATA          PIC X(100).
       FD  STMT-OUT.
       01  STMT-REC.
           05  STM-CUST-ID       PIC X(8).
           05  STM-PERIOD        PIC X(6).
           05  STM-BAL           PIC S9(7)V99 COMP-3.
           05  STM-INTEREST      PIC S9(7)V99 COMP-3.
           05  STM-TOTAL         PIC S9(7)V99 COMP-3.
       WORKING-STORAGE SECTION.
       COPY CUSTREC.
       01  WS-EOF                PIC X(1) VALUE 'N'.
           88  END-OF-INPUT        VALUE 'Y'.
       01  WS-INTEREST           PIC S9(7)V99 COMP-3 VALUE ZERO.
       01  WS-LNK.
           05  LNK-OK            PIC X(1).
           05  LNK-MSG           PIC X(30).
       01  WS-PERIOD             PIC X(6) VALUE 'YYYYMM'.
       01  WS-TXN-TOTAL          PIC S9(7)V99 COMP-3 VALUE ZERO.
       PROCEDURE DIVISION.
       MAIN-SECTION SECTION.
       OPEN-FILES.
           OPEN INPUT  CUST-MASTER
           OPEN OUTPUT STMT-OUT.
       PROCESS-LOOP.
           READ CUST-MASTER
               AT END MOVE 'Y' TO WS-EOF
           END-READ.
           PERFORM PROCESS-CUSTOMER THRU CUSTOMER-DONE
                   UNTIL END-OF-INPUT.
           PERFORM CLOSE-FILES.
           STOP RUN.
       PROCESS-CUSTOMER.
           EXEC SQL
               SELECT COALESCE(SUM(TXN_AMT), 0)
                 INTO :WS-TXN-TOTAL
                 FROM TRANSACTIONS
                WHERE CUST_ID = :CMR-CUST-ID
                  AND TXN_PERIOD = :WS-PERIOD
           END-EXEC.
           CALL 'INTCALC' USING CUST-BAL CUST-RATE WS-INTEREST WS-LNK.
           IF LNK-OK NOT = 'Y'
               DISPLAY 'INTCALC FAILED: ' LNK-MSG
               GO TO CUSTOMER-DONE
           END-IF.
           MOVE CMR-CUST-ID      TO STM-CUST-ID.
           MOVE WS-PERIOD        TO STM-PERIOD.
           MOVE CUST-BAL         TO STM-BAL.
           MOVE WS-INTEREST      TO STM-INTEREST.
           COMPUTE STM-TOTAL = CUST-BAL + WS-INTEREST + WS-TXN-TOTAL.
           WRITE STMT-REC.
           EXEC SQL
               INSERT INTO STATEMENT_HISTORY
                      (CUST_ID, PERIOD, BALANCE, INTEREST, TOTAL)
                 VALUES (:CMR-CUST-ID, :WS-PERIOD, :CUST-BAL,
                         :WS-INTEREST, :STM-TOTAL)
           END-EXEC.
           READ CUST-MASTER
               AT END MOVE 'Y' TO WS-EOF
           END-READ.
       CUSTOMER-DONE.
           EXIT.
       CLOSE-FILES.
           CLOSE CUST-MASTER.
           CLOSE STMT-OUT.
