package com.example.stmtgen.service;

import com.example.stmtgen.dto.CustExtraEmail;
import com.example.stmtgen.dto.CustExtraRaw;
import com.example.stmtgen.dto.CustExtraView;
import com.example.stmtgen.dto.CustomerRec;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;

@Service
public class CustomerRecordParser {

    public CustomerRec parse(String custMasterLine) {
        String padded = rightPad(custMasterLine, 108);

        String custId = padded.substring(0, 8).trim();
        String custLast = padded.substring(8, 38).trim();
        String custFirst = padded.substring(38, 58).trim();
        String custBalRaw = padded.substring(58, 67).trim();
        String custRateRaw = padded.substring(67, 71).trim();
        String custStatus = padded.substring(71, 72).trim();
        String custOpenDate = padded.substring(72, 80).trim();
        String custExtra = padded.substring(80, 104).trim();

        BigDecimal custBal = parseSignedFixed(custBalRaw, 2);
        BigDecimal custRate = parseUnsignedFixed(custRateRaw, 3);

        CustExtraView custExtraView = custExtra.contains("@")
                ? new CustExtraEmail(custExtra)
                : new CustExtraRaw(custExtra);

        return new CustomerRec(
                custId,
                custLast,
                custFirst,
                custBal,
                custRate,
                custStatus,
                custOpenDate,
                custExtra,
                custExtraView
        );
    }

    private BigDecimal parseSignedFixed(String value, int scale) {
        if (value == null || value.isBlank()) {
            return BigDecimal.ZERO.setScale(scale);
        }
        String normalized = value.trim();
        if (normalized.contains(".")) {
            return new BigDecimal(normalized).setScale(scale);
        }
        return new BigDecimal(normalized).movePointLeft(scale).setScale(scale);
    }

    private BigDecimal parseUnsignedFixed(String value, int scale) {
        if (value == null || value.isBlank()) {
            return BigDecimal.ZERO.setScale(scale);
        }
        String normalized = value.trim();
        if (normalized.contains(".")) {
            return new BigDecimal(normalized).setScale(scale);
        }
        return new BigDecimal(normalized).movePointLeft(scale).setScale(scale);
    }

    private String rightPad(String input, int length) {
        String value = input == null ? "" : input;
        if (value.length() >= length) {
            return value;
        }
        return value + " ".repeat(length - value.length());
    }
}
