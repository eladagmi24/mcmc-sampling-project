# Data

The traces are **not committed** — the `fastStorage` directory is 1.19 GB across 1,250 CSV files,
and the dataset is already published by its authors.

Download the **GWA-T-12 Bitbrains** trace from the Grid Workloads Archive
(<http://gwa.ewi.tudelft.nl/datasets/gwa-t-12-bitbrains>) and extract it so that the layout is:

```
data/
    fastStorage/
        2013-8/
            1.csv
            2.csv
            ...
```

The scripts read the first 50 virtual machines and use the earliest 5,000 observations after
feature engineering.

Citation: S. Shen, V. van Beek, and A. Iosup, "Statistical Characterization of Business-Critical
Workloads Hosted in Cloud Datacenters," *CCGrid* 2015.
