package com.example.stmtgen.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.IdClass;
import jakarta.persistence.Table;

import java.io.Serializable;
import java.math.BigDecimal;
import java.util.Objects;

@Entity
@Table(name = "STATEMENT_HISTORY")
@IdClass(StatementHistoryEntity.StatementHistoryId.class)
public class StatementHistoryEntity {

    @Id
    @Column(name = "CUST_ID", length = 8, nullable = false)
    private String custId;

    @Id
    @Column(name = "PERIOD", length = 6, nullable = false)
    private String period;

    @Column(name = "BALANCE", precision = 9, scale = 2, nullable = false)
    private BigDecimal balance;

    @Column(name = "INTEREST", precision = 9, scale = 2, nullable = false)
    private BigDecimal interest;

    @Column(name = "TOTAL", precision = 9, scale = 2, nullable = false)
    private BigDecimal total;

    public StatementHistoryEntity() {
    }

    public StatementHistoryEntity(String custId, String period, BigDecimal balance, BigDecimal interest, BigDecimal total) {
        this.custId = custId;
        this.period = period;
        this.balance = balance;
        this.interest = interest;
        this.total = total;
    }

    public String getCustId() {
        return custId;
    }

    public String getPeriod() {
        return period;
    }

    public BigDecimal getBalance() {
        return balance;
    }

    public BigDecimal getInterest() {
        return interest;
    }

    public BigDecimal getTotal() {
        return total;
    }

    public static class StatementHistoryId implements Serializable {
        private String custId;
        private String period;

        public StatementHistoryId() {
        }

        public StatementHistoryId(String custId, String period) {
            this.custId = custId;
            this.period = period;
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) {
                return true;
            }
            if (!(o instanceof StatementHistoryId that)) {
                return false;
            }
            return Objects.equals(custId, that.custId) && Objects.equals(period, that.period);
        }

        @Override
        public int hashCode() {
            return Objects.hash(custId, period);
        }
    }
}
