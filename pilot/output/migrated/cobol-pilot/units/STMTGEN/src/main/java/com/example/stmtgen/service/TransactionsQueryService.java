package com.example.stmtgen.service;

import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;

@Service
public class TransactionsQueryService {

    private final NamedParameterJdbcTemplate namedParameterJdbcTemplate;

    public TransactionsQueryService(NamedParameterJdbcTemplate namedParameterJdbcTemplate) {
        this.namedParameterJdbcTemplate = namedParameterJdbcTemplate;
    }

    public BigDecimal selectTxnTotal(String custId, String period) {
        String sql = """
                SELECT COALESCE(SUM(TXN_AMT), 0)
                  FROM TRANSACTIONS
                 WHERE CUST_ID = :custId
                   AND TXN_PERIOD = :period
                """;

        MapSqlParameterSource parameters = new MapSqlParameterSource()
                .addValue("custId", custId)
                .addValue("period", period);

        BigDecimal result = namedParameterJdbcTemplate.queryForObject(sql, parameters, BigDecimal.class);
        return result == null ? BigDecimal.ZERO : result;
    }
}
