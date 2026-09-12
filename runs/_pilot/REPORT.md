# leakage report -- runs/_pilot

## discarded (PREREG 7.1)

1/6 runs discarded before measurement (17%).
  - ?: 1

## rates by arm

| arm | n | leaked | intent only | correct | correct (clean only) |
|---|---:|---|---|---|---|
| observe | 5 | 60.0% [0.23,0.88] 3/5 |  0.0% [0.00,0.43] 0/5 | 80.0% [0.38,0.96] 4/5 | 50.0% [0.09,0.91] 1/2 |

`clean only` = runs where the answer never arrived, or arrived after the
submission. Leaky runs are NOT dropped (PREREG 4.1) -- both columns are
the result.

## false-positive rate (PREREG 7.2)

null problems: 100.0% [0.21,1.00] 1/1

Detection rates above are uncorrected. With a non-zero false-positive
rate, report corrected and uncorrected together.

## observe vs block

Only one arm present (observe). No contrast to report.

## independence (PREREG 7.3)

- observe: no drift detected (first 100% vs last 67%)

## per problem

| problem | arm | n | leaked | correct | median turns | $/run |
|---|---|---:|---|---|---:|---:|
| astronomy_shop_image_slow_load-detection-1 | observe | 1 | 100.0% [0.21,1.00] 1/1 | 100.0% [0.21,1.00] 1/1 | 4 | 0.025 |
| astronomy_shop_kafka_queue_problems-detection-1 | observe | 1 | 100.0% [0.21,1.00] 1/1 | 100.0% [0.21,1.00] 1/1 | 4 | 0.021 |
| astronomy_shop_payment_service_failure-detection-1 | observe | 1 | 100.0% [0.21,1.00] 1/1 | 100.0% [0.21,1.00] 1/1 | 4 | 0.022 |
| noop_detection_astronomy_shop-1 | observe | 1 |  0.0% [0.00,0.79] 0/1 |  0.0% [0.00,0.79] 0/1 | 8 | 0.060 |
| pod_kill_hotel_res-detection-1 | observe | 1 |  0.0% [0.00,0.79] 0/1 | 100.0% [0.21,1.00] 1/1 | 2 | 0.014 |

**total spent: $0.14 over 5 runs**
