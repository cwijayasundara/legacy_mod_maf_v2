package com.example.stmtgen.tasklet;

import com.example.stmtgen.dto.CustomerRec;
import com.example.stmtgen.dto.StmtRec;
import com.example.stmtgen.intcalc.IntcalcResult;
import com.example.stmtgen.intcalc.IntcalcService;
import com.example.stmtgen.intcalc.LnkStatus;
import com.example.stmtgen.repository.StatementHistoryRepository;
import com.example.stmtgen.service.StmtgenFileService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.Optional;

@Component
public class ProcessCustomerTasklet {

    private static final MathContext MC = new MathContext(18, RoundingMode.HALF_UP);

    private final StmtgenFileService stmtgenFileService;
    private final StatementHistoryRepository statementHistoryRepository;
    private final IntcalcService intcalcService;
    private final String stmtPeriod;

    public ProcessCustomerTasklet(
            StmtgenFileService stmtgenFileService,
            StatementHistoryRepository statementHistoryRepository,
            IntcalcService intcalcService,
            @Value("${stmtgen.period:202401}") String stmtPeriod
    ) {
        this.stmtgenFileService = stmtgenFileService;
        this.statementHistoryRepository = statementHistoryRepository;
        this.intcalcService = intcalcService;
        this.stmtPeriod = stmtPeriod;
    }

    public void processAllCustomers() {
        Optional<CustomerRec> current = stmtgenFileService.readCustomer();
        while (current.isPresent()) {
            processCustomer(current.get());
            current = stmtgenFileService.readCustomer();
        }
    }

    private void processCustomer(CustomerRec customerRec) {
        BigDecimal wsTxnTotal = statementHistoryRepository.selectTransactionTotal(
                customerRec.custId(),
                stmtPeriod
        );

        IntcalcResult intcalcResult = intcalcService.calculateInterest(
                customerRec.custBal(),
                customerRec.custRate(),
                BigDecimal.ZERO,
                new LnkStatus("Y", "OK")
        );

        if (!"Y".equals(intcalcResult.lnkStatus().lnkOk())) {
            System.out.println("INTCALC FAILED: " + intcalcResult.lnkStatus().lnkMsg());
            return;
        }

        BigDecimal stmTotal = customerRec.custBal()
                .add(intcalcResult.lnkInterest(), MC)
                .add(wsTxnTotal, MC);

        StmtRec stmtRec = new StmtRec(
                customerRec.custId(),
                stmtPeriod,
                customerRec.custBal(),
                intcalcResult.lnkInterest(),
                stmTotal
        );

        stmtgenFileService.writeStatement(stmtRec);

        statementHistoryRepository.insertStatementHistory(
                customerRec.custId(),
                stmtPeriod,
                customerRec.custBal(),
                intcalcResult.lnkInterest(),
                stmTotal
        );
    }
}
