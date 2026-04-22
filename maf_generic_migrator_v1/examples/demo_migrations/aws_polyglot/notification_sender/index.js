"use strict";

const { SNSClient, PublishCommand } = require("@aws-sdk/client-sns");

const sns = new SNSClient({});

/**
 * Consume SQS messages from the downstream queue and publish a notification
 * for each accepted order.
 */
exports.handler = async (event) => {
  const topicArn = process.env.TOPIC_ARN;
  let sent = 0;

  for (const record of event.Records || []) {
    const body = JSON.parse(record.body || "{}");
    if (body.event !== "order_accepted") continue;

    await sns.send(
      new PublishCommand({
        TopicArn: topicArn,
        Subject: "Order accepted",
        Message: JSON.stringify({ orderId: body.orderId, at: new Date().toISOString() }),
      })
    );
    sent += 1;
  }

  return { statusCode: 200, body: JSON.stringify({ sent }) };
};
