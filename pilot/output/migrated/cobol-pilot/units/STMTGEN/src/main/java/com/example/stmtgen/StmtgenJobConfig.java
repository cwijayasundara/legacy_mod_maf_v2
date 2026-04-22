package com.example.stmtgen;

import com.example.stmtgen.tasklet.CloseFilesTasklet;
import com.example.stmtgen.tasklet.OpenFilesTasklet;
import com.example.stmtgen.tasklet.ProcessCustomerTasklet;
import org.springframework.batch.core.Job;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.job.builder.JobBuilder;
import org.springframework.batch.core.repository.JobRepository;
import org.springframework.batch.core.step.builder.StepBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.transaction.PlatformTransactionManager;

@Configuration
public class StmtgenJobConfig {

    @Bean
    public Job STMTGEN(
            JobRepository jobRepository,
            Step mainSectionStep
    ) {
        return new JobBuilder("STMTGEN", jobRepository)
                .start(mainSectionStep)
                .build();
    }

    @Bean
    public Step mainSectionStep(
            JobRepository jobRepository,
            PlatformTransactionManager transactionManager,
            OpenFilesTasklet openFilesTasklet,
            ProcessCustomerTasklet processCustomerTasklet,
            CloseFilesTasklet closeFilesTasklet
    ) {
        return new StepBuilder("MAIN-SECTION", jobRepository)
                .tasklet((contribution, chunkContext) -> {
                    openFilesTasklet.open();
                    try {
                        processCustomerTasklet.processAllCustomers();
                    } finally {
                        closeFilesTasklet.close();
                    }
                    return org.springframework.batch.repeat.RepeatStatus.FINISHED;
                }, transactionManager)
                .build();
    }
}
