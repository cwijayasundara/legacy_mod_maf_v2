package com.example.stmtgen.service;

import com.example.stmtgen.dto.CustExtraEmail;
import com.example.stmtgen.dto.CustExtraRaw;
import com.example.stmtgen.dto.CustExtraView;
import com.example.stmtgen.dto.CustomerRec;
import com.example.stmtgen.dto.StmtRec;
import jakarta.annotation.PreDestroy;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Optional;

@Service
public class StmtgenFileService {

    private final Path inputPath;
    private final Path outputPath;

    private BufferedReader reader;
    private BufferedWriter writer;

    public StmtgenFileService(
            @Value("${stmtgen.input.path:data/PROD.CUST.MASTER}") String inputPath,
            @Value("${stmtgen.output.path:data/PROD.STMT.DAILY}") String outputPath
    ) {
        this.inputPath = Path.of(inputPath);
        this.outputPath = Path.of(outputPath);
    }

    public void openInput() {
        try {
            if (!Files.exists(inputPath)) {
                throw new IllegalStateException("Input dataset not found: " + inputPath);
            }
            reader = Files.newBufferedReader(inputPath);
        } catch (IOException e) {
            throw new IllegalStateException("Failed to open input dataset: " + inputPath, e);
        }
    }

    public void openOutput() {
        try {
            Path parent = outputPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            writer = Files.newBufferedWriter(
                    outputPath,
                    StandardOpenOption.CREATE,
                    StandardOpenOption.TRUNCATE_EXISTING,
                    StandardOpenOption.WRITE
            );
        } catch (IOException e) {
            throw new IllegalStateException("Failed to open output dataset: " + outputPath, e);
        }
    }

    public Optional<CustomerRec> readCustomer() {
        ensureReaderOpen();
        try {
            String line = reader.readLine();
            if (line == null) {
                return Optional.empty();
            }
            return Optional.of(parseCustomer(line));
        } catch (IOException e) {
            throw new IllegalStateException("Failed reading input dataset: " + inputPath, e);
        }
    }

    public void writeStatement(StmtRec stmtRec) {
        ensureWriterOpen();
        try {
            writer.write(formatStatement(stmtRec));
            writer.newLine();
            writer.flush();
        } catch (IOException e) {
            throw new IllegalStateException("Failed writing output dataset: " + outputPath, e);
        }
    }

    public void closeInput() {
        if (reader != null) {
            try {
                reader.close();
            } catch (IOException e) {
                throw new IllegalStateException("Failed to close input dataset: " + inputPath, e);
            } finally {
                reader = null;
            }
        }
    }

    public void closeOutput() {
        if (writer != null) {
            try {
                writer.close();
            } catch (IOException e) {
                throw new IllegalStateException("Failed to close output dataset: " + outputPath, e);
            } finally {
                writer = null;
            }
        }
    }

    @PreDestroy
    public void cleanup() {
        closeInput();
        closeOutput();
    }

    private void ensureReaderOpen() {
        if (reader == null) {
            throw new IllegalStateException("Input dataset is not open");
        }
    }

    private void ensureWriterOpen() {
        if (writer == null) {
            throw new IllegalStateException("Output dataset is not open");
        }
    }

    private CustomerRec parseCustomer(String line) {
        String[] parts = line.split("\\|", -1);
        String custId = value(parts, 0, 8);
        String custLast = value(parts, 1, 30);
        String custFirst = value(parts, 2, 20);
        BigDecimal custBal = decimal(value(parts, 3, 32));
        BigDecimal custRate = decimal(value(parts, 4, 16));
        String custStatus = value(parts, 5, 1);
        String custOpenDate = value(parts, 6, 8);
        String custExtra = value(parts, 7, 24);

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

    private String formatStatement(StmtRec stmtRec) {
        return String.join("|",
                safe(stmtRec.stmCustId()),
                safe(stmtRec.stmPeriod()),
                stmtRec.stmBal().toPlainString(),
                stmtRec.stmInterest().toPlainString(),
                stmtRec.stmTotal().toPlainString()
        );
    }

    private String value(String[] parts, int index, int max) {
        if (index >= parts.length) {
            return "";
        }
        String value = parts[index] == null ? "" : parts[index].trim();
        return value.length() > max ? value.substring(0, max) : value;
    }

    private BigDecimal decimal(String value) {
        if (value == null || value.isBlank()) {
            return BigDecimal.ZERO;
        }
        return new BigDecimal(value.trim());
    }

    private String safe(String value) {
        return value == null ? "" : value;
    }
}
