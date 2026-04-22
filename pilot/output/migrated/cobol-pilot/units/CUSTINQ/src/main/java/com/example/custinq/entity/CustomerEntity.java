package com.example.custinq.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.math.BigDecimal;

@Entity
@Table(name = "CUSTOMER")
public class CustomerEntity {

    @Id
    @Column(name = "CUST_ID", length = 8, nullable = false)
    private String custId;

    @Column(name = "CUST_FIRST", length = 20)
    private String custFirst;

    @Column(name = "CUST_LAST", length = 30)
    private String custLast;

    @Column(name = "CUST_BAL", precision = 9, scale = 2)
    private BigDecimal custBal;

    @Column(name = "CUST_STATUS", length = 1)
    private String custStatus;

    @Column(name = "CUST_RATE", precision = 4, scale = 3)
    private BigDecimal custRate;

    @Column(name = "CUST_OPEN_DATE", length = 8)
    private String custOpenDate;

    @Column(name = "CUST_EXTRA", length = 24)
    private String custExtra;

    public String getCustId() {
        return custId;
    }

    public void setCustId(String custId) {
        this.custId = custId;
    }

    public String getCustFirst() {
        return custFirst;
    }

    public void setCustFirst(String custFirst) {
        this.custFirst = custFirst;
    }

    public String getCustLast() {
        return custLast;
    }

    public void setCustLast(String custLast) {
        this.custLast = custLast;
    }

    public BigDecimal getCustBal() {
        return custBal;
    }

    public void setCustBal(BigDecimal custBal) {
        this.custBal = custBal;
    }

    public String getCustStatus() {
        return custStatus;
    }

    public void setCustStatus(String custStatus) {
        this.custStatus = custStatus;
    }

    public BigDecimal getCustRate() {
        return custRate;
    }

    public void setCustRate(BigDecimal custRate) {
        this.custRate = custRate;
    }

    public String getCustOpenDate() {
        return custOpenDate;
    }

    public void setCustOpenDate(String custOpenDate) {
        this.custOpenDate = custOpenDate;
    }

    public String getCustExtra() {
        return custExtra;
    }

    public void setCustExtra(String custExtra) {
        this.custExtra = custExtra;
    }
}
