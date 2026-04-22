      ******************************************************************
      * CUSTREC — customer master record layout (shared copybook).
      * Mirrors the shape used by CUSTINQ and STMTGEN.
      ******************************************************************
       01  CUSTOMER-REC.
           05  CUST-ID         PIC X(8).
           05  CUST-NAME.
               10  CUST-FIRST  PIC X(20).
               10  CUST-LAST   PIC X(30).
           05  CUST-BAL        PIC S9(7)V99 COMP-3.
           05  CUST-RATE       PIC 9(1)V999 COMP-3.
           05  CUST-STATUS     PIC X(1).
               88  ACTIVE        VALUE 'A'.
               88  SUSPENDED     VALUE 'S'.
               88  CLOSED        VALUE 'C'.
           05  CUST-OPEN-DATE  PIC X(8).
           05  CUST-EXTRA      PIC X(24).
           05  CUST-EXTRA-R    REDEFINES CUST-EXTRA.
               10  CUST-EMAIL  PIC X(24).
