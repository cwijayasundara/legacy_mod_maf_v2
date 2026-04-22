package com.example.custinq.dto;

import jakarta.validation.constraints.Digits;
import jakarta.validation.constraints.Size;

import java.math.BigDecimal;

public record CustomerRec(
        @Size(max = 8)
        String custId,
        @Size(max = 20)
        String custFirst,
        @Size(max = 30)
        String custLast,
        @Digits(integer = 7, fraction = 2)
        BigDecimal custBal,
        @Size(max = 1)
        String custStatus,
        @Digits(integer = 1, fraction = 3)
        BigDecimal custRate,
        @Size(max = 8)
        String custOpenDate,
        @Size(max = 24)
        String custExtra,
        @Size(max = 24)
        String custEmail
) {
}
