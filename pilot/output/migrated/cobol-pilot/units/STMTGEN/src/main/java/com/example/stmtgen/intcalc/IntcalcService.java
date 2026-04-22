package com.example.stmtgen.intcalc;

import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;

@Service
public class IntcalcService {

    private static final MathContext MC = new MathContext(18, RoundingMode.HALF_UP);

    public IntcalcResult calculateInterest(
            BigDecimal lnkBalance,
            BigDecimal lnkRate,
            BigDecimal lnkInterest,
            LnkStatus lnkStatus
    ) {
        if (lnkBalance.compareTo(BigDecimal.ZERO) < 0) {
            return new IntcalcResult(
                    lnkInterest,
                    new LnkStatus("N", "NEGATIVE BALANCE")
            );
        }

        if (lnkRate.compareTo(BigDecimal.ZERO) < 0 || lnkRate.compareTo(BigDecimal.ONE) > 0) {
            return new IntcalcResult(
                    lnkInterest,
                    new LnkStatus("N", "RATE OUT OF RANGE")
            );
        }

        BigDecimal wsTemp = lnkBalance.multiply(lnkRate, MC);

        return new IntcalcResult(
                wsTemp,
                new LnkStatus("Y", "OK")
        );
    }
}
