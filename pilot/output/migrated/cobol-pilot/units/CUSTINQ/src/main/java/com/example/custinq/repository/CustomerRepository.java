package com.example.custinq.repository;

import com.example.custinq.dto.CustomerRec;
import com.example.custinq.entity.CustomerEntity;
import org.springframework.stereotype.Repository;

import java.util.Optional;

@Repository
public class CustomerRepository {

    private final CustomerJpaRepository customerJpaRepository;

    public CustomerRepository(CustomerJpaRepository customerJpaRepository) {
        this.customerJpaRepository = customerJpaRepository;
    }

    public Optional<CustomerRec> findCustomerByCustId(String custId) {
        return customerJpaRepository.findById(custId).map(this::toDto);
    }

    private CustomerRec toDto(CustomerEntity entity) {
        String extra = entity.getCustExtra();
        String email = extra;
        return new CustomerRec(
                entity.getCustId(),
                entity.getCustFirst(),
                entity.getCustLast(),
                entity.getCustBal(),
                entity.getCustStatus(),
                entity.getCustRate(),
                entity.getCustOpenDate(),
                extra,
                email
        );
    }
}
