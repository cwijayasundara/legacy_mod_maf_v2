package com.example.stmtgen.repository;

import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

import java.math.BigDecimal;

@Repository
public class StatementHistoryRepository {

    private final NamedParameterJdbcTemplate namedParameterJdbcTemplate;

    public StatementHistoryRepository(NamedParameterJdbcTemplate namedParameterJdbcTemplate) {
        this.namedParameterJdbcTemplate = namedParameterJdbcTemplate;
    }

    public BigDecimal selectTransactionTotal(String custId, String period) {
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

    public void insertStatementHistory(
            String custId,
            String period,
            BigDecimal balance,
            BigDecimal interest,
            BigDecimal total
    ) {
        String sql = """
                INSERT INTO STATEMENT_HISTORY
                       (CUST_ID, PERIOD, BALANCE, INTEREST, TOTAL)
                  VALUES (:custId, :period, :balance, :interest, :total)
                """;

        MapSqlParameterSource parameters = new MapSqlParameterSource()
                .addValue("custId", custId)
                .addValue("period", period)
                .addValue("balance", balance)
                .addValue("interest", interest)
                .addValue("total", total);

        namedParameterJdbcTemplate.update(sql, parameters);
    }
}
