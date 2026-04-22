       IDENTIFICATION DIVISION.
       PROGRAM-ID. INTCALC.
      *   Subroutine: compute interest on a balance at a rate.
      *   No JCL, no CICS — pure callee via CALL 'INTCALC' USING ...
       ENVIRONMENT DIVISION.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-TEMP              PIC S9(9)V99 COMP-3 VALUE ZERO.
       LINKAGE SECTION.
       01  LNK-BALANCE          PIC S9(7)V99 COMP-3.
       01  LNK-RATE             PIC 9(1)V999 COMP-3.
       01  LNK-INTEREST         PIC S9(7)V99 COMP-3.
       01  LNK-STATUS.
           05  LNK-OK           PIC X(1).
           05  LNK-MSG          PIC X(30).
       PROCEDURE DIVISION USING LNK-BALANCE LNK-RATE LNK-INTEREST LNK-STATUS.
       MAIN-SECTION SECTION.
       VALIDATE-INPUTS.
           IF LNK-BALANCE < 0
               MOVE 'N' TO LNK-OK
               MOVE 'NEGATIVE BALANCE'   TO LNK-MSG
               GOBACK
           END-IF.
           IF LNK-RATE < 0 OR LNK-RATE > 1
               MOVE 'N' TO LNK-OK
               MOVE 'RATE OUT OF RANGE'  TO LNK-MSG
               GOBACK
           END-IF.
       COMPUTE-INTEREST.
           COMPUTE WS-TEMP = LNK-BALANCE * LNK-RATE.
           MOVE WS-TEMP TO LNK-INTEREST.
           MOVE 'Y' TO LNK-OK.
           MOVE 'OK' TO LNK-MSG.
           GOBACK.
