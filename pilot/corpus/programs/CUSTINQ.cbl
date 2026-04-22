       IDENTIFICATION DIVISION.
       PROGRAM-ID. CUSTINQ.
      *   CICS customer inquiry: receive COMMAREA with CUST-ID, look up
      *   in DB2, return customer details on the response map.
       ENVIRONMENT DIVISION.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY CUSTREC.
       01  WS-SQLCODE           PIC S9(9) COMP VALUE ZERO.
       01  WS-RESP              PIC S9(9) COMP VALUE ZERO.
       01  WS-MESSAGE           PIC X(50).
       LINKAGE SECTION.
       01  COMMAREA.
           05  COMM-CUST-ID     PIC X(8).
           05  COMM-ACTION      PIC X(1).
           05  COMM-RESULT      PIC X(1).
           05  COMM-CUST-OUT    PIC X(80).
       PROCEDURE DIVISION USING COMMAREA.
       MAIN-SECTION SECTION.
       ENTRY-POINT.
           EXEC CICS RECEIVE MAP('CUSTMAP')
                MAPSET('CUSTMST')
                INTO(COMMAREA)
                RESP(WS-RESP)
           END-EXEC.
           IF WS-RESP NOT = DFHRESP(NORMAL)
               MOVE 'E' TO COMM-RESULT
               PERFORM SEND-ERROR
           ELSE
               PERFORM LOOKUP-CUSTOMER
           END-IF.
       LOOKUP-CUSTOMER.
           EXEC SQL
               SELECT CUST_ID, CUST_FIRST, CUST_LAST, CUST_BAL, CUST_STATUS
                 INTO :CUST-ID, :CUST-FIRST, :CUST-LAST, :CUST-BAL, :CUST-STATUS
                 FROM CUSTOMER
                WHERE CUST_ID = :COMM-CUST-ID
           END-EXEC.
           IF SQLCODE = 0
               IF ACTIVE
                   MOVE 'A' TO COMM-RESULT
                   PERFORM SEND-RESPONSE
               ELSE
                   MOVE 'S' TO COMM-RESULT
                   PERFORM SEND-SUSPENDED
               END-IF
           ELSE
               MOVE 'N' TO COMM-RESULT
               PERFORM SEND-NOT-FOUND
           END-IF.
       SEND-RESPONSE.
           STRING CUST-FIRST DELIMITED BY SPACE
                  ' ' DELIMITED BY SIZE
                  CUST-LAST  DELIMITED BY SPACE
                  INTO COMM-CUST-OUT.
           EXEC CICS SEND MAP('CUSTMAP')
                MAPSET('CUSTMST')
                FROM(COMMAREA)
                RESP(WS-RESP)
           END-EXEC.
       SEND-SUSPENDED.
           MOVE 'ACCOUNT SUSPENDED' TO COMM-CUST-OUT.
           EXEC CICS SEND TEXT FROM(COMM-CUST-OUT)
                RESP(WS-RESP)
           END-EXEC.
       SEND-NOT-FOUND.
           MOVE 'CUSTOMER NOT FOUND' TO COMM-CUST-OUT.
           EXEC CICS SEND TEXT FROM(COMM-CUST-OUT)
                RESP(WS-RESP)
           END-EXEC.
       SEND-ERROR.
           MOVE 'RECEIVE FAILED'    TO COMM-CUST-OUT.
           EXEC CICS SEND TEXT FROM(COMM-CUST-OUT)
                RESP(WS-RESP)
           END-EXEC.
           EXEC CICS RETURN
           END-EXEC.
