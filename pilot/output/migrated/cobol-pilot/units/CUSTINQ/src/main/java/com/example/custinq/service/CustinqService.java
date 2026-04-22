package com.example.custinq.service;

import com.example.custinq.dto.CustinqRequest;
import com.example.custinq.dto.CustinqResponse;
import com.example.custinq.dto.CustomerRec;
import com.example.custinq.repository.CustomerRepository;
import org.springframework.stereotype.Service;

import java.math.BigDecimal;
import java.util.Optional;

@Service
public class CustinqService {

    private final CustomerRepository customerRepository;

    public CustinqService(CustomerRepository customerRepository) {
        this.customerRepository = customerRepository;
    }

    public CustinqResponse inquireCustomer(CustinqRequest request, String aid) {
        if (aid != null && !aid.isBlank()) {
            // TODO: AID key branching beyond normal ENTER flow should be verified against BMS map behavior.
        }

        if (request.commCustId() == null || request.commCustId().isBlank()) {
            return new CustinqResponse(
                    request.commCustId(),
                    "E",
                    "RECEIVE FAILED",
                    null
            );
        }

        Optional<CustomerRec> customerOptional = customerRepository.findCustomerByCustId(request.commCustId());

        if (customerOptional.isEmpty()) {
            return new CustinqResponse(
                    request.commCustId(),
                    "N",
                    "CUSTOMER NOT FOUND",
                    null
            );
        }

        CustomerRec customer = customerOptional.get();
        if ("A".equals(customer.custStatus())) {
            String fullName = buildCustomerOutput(customer.custFirst(), customer.custLast());
            return new CustinqResponse(
                    request.commCustId(),
                    "A",
                    fullName,
                    customer
            );
        }

        return new CustinqResponse(
                request.commCustId(),
                "S",
                "ACCOUNT SUSPENDED",
                customer
        );
    }

    private String buildCustomerOutput(String firstName, String lastName) {
        String first = firstName == null ? "" : firstName.stripTrailing();
        String last = lastName == null ? "" : lastName.stripTrailing();
        return (first + " " + last).trim();
    }
}
