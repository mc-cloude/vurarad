"""
create_radiogenomics_schema.py
==============================
VuraRAD Radiogenomics Module — Schema v2.0
Provisions the `radiogenomics` BigQuery dataset + all tables,
and the `vura_core` dataset + Master Patient Index (MPI) table.

Improvements (v2.0):
  - A. Master Patient Index (vura_core.master_patient_index)
  - B. Flat BOOL mutation columns in external_genomic_labels
  - E. DSO Seal + pathology feedback fields in virtual_biopsy_reports
  - F. Multi-lesion support in radiomics_features
  - G. Label provenance fields in external_genomic_labels
  - H. Model registry table (radiogenomics.model_registry)

Run once before ingestion, or re-run anytime (fully idempotent).
Usage:
    python jobs/create_radiogenomics_schema.py
"""

import os
import json
import logging
from datetime import datetime
from google.cloud import bigquery

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - SCHEMA_V2 - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

PROJECT_ID      = os.environ.get("GCP_PROJECT_ID", "vurarad")
DATASET_ID      = "radiogenomics"
CORE_DATASET_ID = "vura_core"           # New: Cross-system master data
DATASET_REGION  = os.environ.get("GCP_REGION", "me-central1")


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA DEFINITIONS v2.0
# ─────────────────────────────────────────────────────────────────────────────

# ── A. Master Patient Index ──────────────────────────────────────────────────
SCHEMA_MASTER_PATIENT_INDEX = [
    bigquery.SchemaField("vura_patient_id",      "STRING",    mode="REQUIRED"),  # Canonical: VUR-2026-XXXXX
    bigquery.SchemaField("fhir_patient_id",      "STRING",    mode="NULLABLE"),  # Google Healthcare API resource ID
    bigquery.SchemaField("firestore_patient_id", "STRING",    mode="NULLABLE"),  # Firestore patients/{id}
    bigquery.SchemaField("firestore_study_uids", "STRING",    mode="REPEATED"),  # All Firestore study UIDs
    bigquery.SchemaField("case_uids",            "STRING",    mode="REPEATED"),  # All BigQuery case_uids
    bigquery.SchemaField("cohort_region",        "STRING",    mode="NULLABLE"),  # KSA | KE | TCIA | TCGA | BCBM
    bigquery.SchemaField("data_tier",            "STRING",    mode="NULLABLE"),  # CLINICAL | RESEARCH
    bigquery.SchemaField("de_identified",        "BOOL",      mode="NULLABLE"),  # Whether PHI was stripped
    bigquery.SchemaField("created_at",           "TIMESTAMP", mode="NULLABLE"),
    bigquery.SchemaField("updated_at",           "TIMESTAMP", mode="NULLABLE"),
]

# ── clinical_cohorts (unchanged, keeps existing data) ────────────────────────
SCHEMA_CLINICAL_COHORTS = [
    bigquery.SchemaField("case_uid",              "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("cohort_region",         "STRING",    mode="NULLABLE"),   # KE, SA, TCIA, TCGA, BCBM
    bigquery.SchemaField("cancer_type",           "STRING",    mode="NULLABLE"),   # NSCLC, HCC, BREAST, GBM…
    bigquery.SchemaField("modality",              "STRING",    mode="NULLABLE"),   # CT, MRI, PET-CT
    bigquery.SchemaField("body_part",             "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("institution_id",        "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("acquisition_date",      "DATE",      mode="NULLABLE"),
    bigquery.SchemaField("ground_truth_genotype", "JSON",      mode="NULLABLE"),
    bigquery.SchemaField("data_source",           "STRING",    mode="NULLABLE"),   # CLINICAL | TCIA | TCGA | BCBM
    bigquery.SchemaField("data_tier",             "STRING",    mode="NULLABLE"),   # TRAINING | VALIDATION | INFERENCE
    bigquery.SchemaField("imaging_gcs_prefix",    "STRING",    mode="NULLABLE"),   # v2: gs://vura-radiomics/...
    bigquery.SchemaField("vura_patient_id",       "STRING",    mode="NULLABLE"),   # v2: FK to MPI
    bigquery.SchemaField("de_identified",         "BOOL",      mode="NULLABLE"),   # v2: PHI strip flag
    bigquery.SchemaField("ingested_at",           "TIMESTAMP", mode="NULLABLE"),
]

# ── F. radiomics_features — multi-lesion support ─────────────────────────────
SCHEMA_RADIOMICS_FEATURES = [
    bigquery.SchemaField("feature_id",               "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("case_uid",                 "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("lesion_id",                "STRING",    mode="NULLABLE"),   # v2: e.g. "R01-001_L1"
    bigquery.SchemaField("lesion_rank",              "INTEGER",   mode="NULLABLE"),   # v2: 1=dominant/largest
    bigquery.SchemaField("lesion_location",          "STRING",    mode="NULLABLE"),   # v2: "RUL", "LLL", "RML"
    bigquery.SchemaField("cohort_region",            "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("lesion_volume_mm3",        "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("shape_sphericity",         "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("shape_elongation",         "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("shape_flatness",           "FLOAT64",   mode="NULLABLE"),   # v2
    bigquery.SchemaField("texture_entropy",          "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("texture_energy",           "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("texture_correlation",      "FLOAT64",   mode="NULLABLE"),   # v2
    bigquery.SchemaField("intensity_mean",           "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("intensity_kurtosis",       "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("intensity_skewness",       "FLOAT64",   mode="NULLABLE"),   # v2
    bigquery.SchemaField("wavelet_features",         "JSON",      mode="NULLABLE"),   # 64-dim vector
    bigquery.SchemaField("deep_features",            "JSON",      mode="NULLABLE"),   # 512-dim MONAI embedding
    bigquery.SchemaField("gcs_pending",              "BOOL",      mode="NULLABLE"),   # True = stub, CT not downloaded
    bigquery.SchemaField("extraction_model_version", "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("extracted_at",             "TIMESTAMP", mode="NULLABLE"),
]

# ── B+G. external_genomic_labels — flat mutations + provenance ───────────────
SCHEMA_EXTERNAL_GENOMIC_LABELS = [
    bigquery.SchemaField("label_id",              "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("case_uid",              "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("dataset_source",        "STRING",    mode="NULLABLE"),   # TCIA_NSCLC | TCGA | BCBM
    bigquery.SchemaField("rnaseq_signature",      "JSON",      mode="NULLABLE"),   # RNA-Seq expression vector
    bigquery.SchemaField("mutation_labels",       "JSON",      mode="NULLABLE"),   # Full JSON blob (legacy compat)
    # B. Flat mutation BOOL columns (queryable without JSON_VALUE):
    bigquery.SchemaField("egfr_mutant",           "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("kras_mutant",           "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("alk_positive",          "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("tp53_mutant",           "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("braf_mutant",           "BOOL",      mode="NULLABLE"),   # Common in NSCLC subsets
    # Clinical subtypes:
    bigquery.SchemaField("molecular_subtype",     "STRING",    mode="NULLABLE"),   # LUAD | LUSC | TNBC | HER2+
    bigquery.SchemaField("ihc_er",                "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("ihc_pr",                "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("ihc_her2",              "BOOL",      mode="NULLABLE"),
    bigquery.SchemaField("tumour_purity",         "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("segmentation_mask_gcs", "STRING",    mode="NULLABLE"),   # GCS path to NIfTI mask (BCBM)
    # G. Label provenance:
    bigquery.SchemaField("label_confidence",      "FLOAT64",   mode="NULLABLE"),   # 0-1, source reliability
    bigquery.SchemaField("label_source_method",   "STRING",    mode="NULLABLE"),   # NGS | IHC | LITERATURE | CLINICAL
    bigquery.SchemaField("label_version",         "INTEGER",   mode="NULLABLE"),   # Versioning for re-annotations
    bigquery.SchemaField("validated_by",          "STRING",    mode="NULLABLE"),   # Institution or validator ID
    bigquery.SchemaField("dataset_access_tier",   "STRING",    mode="NULLABLE"),   # OPEN | TIERED
    bigquery.SchemaField("ingested_at",           "TIMESTAMP", mode="NULLABLE"),
]

# ── B. genomic_predictions — flat probability columns ────────────────────────
SCHEMA_GENOMIC_PREDICTIONS = [
    bigquery.SchemaField("prediction_id",               "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("case_uid",                    "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("lesion_id",                   "STRING",    mode="NULLABLE"),   # v2: multi-lesion link
    bigquery.SchemaField("cohort_region",               "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("predicted_mutation_profile",  "JSON",      mode="NULLABLE"),   # Full JSON blob (legacy)
    # B. Flat probability columns:
    bigquery.SchemaField("egfr_probability",            "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("kras_probability",            "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("alk_probability",             "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("tp53_probability",            "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("dominant_mutation",           "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("cancer_subtype_predicted",    "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("confidence_score",            "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("ihc_proxy_estrogen",          "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("ihc_proxy_progesterone",      "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("ihc_proxy_her2",              "FLOAT64",   mode="NULLABLE"),
    # H. Link to model that produced this prediction:
    bigquery.SchemaField("model_id",                   "STRING",    mode="NULLABLE"),   # FK to model_registry
    bigquery.SchemaField("model_version",               "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("predicted_at",               "TIMESTAMP", mode="NULLABLE"),
]
# ── Canonical DICOM Index (Vision 2026: Data Platform) ──────────────────────
SCHEMA_DICOM_CANONICAL_INDEX = [
    bigquery.SchemaField("sop_instance_uid",   "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("series_instance_uid", "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("study_instance_uid",  "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("patient_id",         "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("modality",           "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("study_date",         "DATE",      mode="NULLABLE"),
    bigquery.SchemaField("body_part_examined", "STRING",    mode="NULLABLE"),
    # High-fidelity metadata (DICOM Part 3 Tag mirroring)
    bigquery.SchemaField("full_metadata",      "JSON",      mode="NULLABLE"),   # Full tag dump
    bigquery.SchemaField("pixel_spacing",      "STRING",    mode="NULLABLE"),   # [0.7, 0.7]
    bigquery.SchemaField("slice_thickness",    "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("kvp",                "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("exposure_time",      "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("gcs_path",           "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("ingested_at",        "TIMESTAMP", mode="NULLABLE"),
]

# ── H. Model Registry ─────────────────────────────────────────────────────────
SCHEMA_MODEL_REGISTRY = [
    bigquery.SchemaField("model_id",          "STRING",    mode="REQUIRED"),   # "egfr_mlp_v1"
    bigquery.SchemaField("model_type",        "STRING",    mode="NULLABLE"),   # MLP | RandomForest | MONAI_BUNDLE | XGB
    bigquery.SchemaField("target_gene",       "STRING",    mode="NULLABLE"),   # EGFR | KRAS | ALK | TP53 | MULTI
    bigquery.SchemaField("target_task",       "STRING",    mode="NULLABLE"),   # MUTATION_CLASSIFIER | SUBTYPE | SURVIVAL
    bigquery.SchemaField("auc_val",           "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("auc_train",         "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("f1_score",          "FLOAT64",   mode="NULLABLE"),
    bigquery.SchemaField("training_cohort",   "STRING",    mode="NULLABLE"),   # "TCIA_NSCLC_211"
    bigquery.SchemaField("n_samples",         "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("feature_count",     "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("feature_names",     "JSON",      mode="NULLABLE"),   # List of feature names used
    bigquery.SchemaField("hyperparameters",   "JSON",      mode="NULLABLE"),   # Training config
    bigquery.SchemaField("gcs_path",          "STRING",    mode="NULLABLE"),   # gs://vurarad-models/...
    bigquery.SchemaField("deployed_at",       "TIMESTAMP", mode="NULLABLE"),
    bigquery.SchemaField("status",            "STRING",    mode="NULLABLE"),   # ACTIVE | DEPRECATED | STAGING
    bigquery.SchemaField("notes",             "STRING",    mode="NULLABLE"),
]

# ── Table registry ────────────────────────────────────────────────────────────
RADIOGENOMICS_TABLES = {
    "clinical_cohorts":        SCHEMA_CLINICAL_COHORTS,
    "radiomics_features":      SCHEMA_RADIOMICS_FEATURES,
    "genomic_predictions":     SCHEMA_GENOMIC_PREDICTIONS,
    "virtual_biopsy_reports":  SCHEMA_VIRTUAL_BIOPSY_REPORTS,
    "external_genomic_labels": SCHEMA_EXTERNAL_GENOMIC_LABELS,
    "model_registry":          SCHEMA_MODEL_REGISTRY,
    "dicom_canonical_index":   SCHEMA_DICOM_CANONICAL_INDEX, # Vision 2026
}

CORE_TABLES = {
    "master_patient_index": SCHEMA_MASTER_PATIENT_INDEX,
}


# ─────────────────────────────────────────────────────────────────────────────
# PROVISIONING LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def ensure_dataset(client: bigquery.Client, dataset_id: str) -> bigquery.DatasetReference:
    dataset_ref = client.dataset(dataset_id)
    try:
        client.get_dataset(dataset_ref)
        logger.info(f"[OK] Dataset '{dataset_id}' already exists.")
    except Exception:
        logger.info(f"[CREATE] Dataset '{dataset_id}' not found — creating in {DATASET_REGION}...")
        dataset = bigquery.Dataset(f"{PROJECT_ID}.{dataset_id}")
        dataset.location = DATASET_REGION
        dataset.description = (
            "VuraRAD core master data — Master Patient Index (MPI), "
            "cross-system patient linkage for FHIR, Firestore, and BigQuery."
            if dataset_id == CORE_DATASET_ID else
            "VuraRAD Radiogenomics — virtual biopsy training data warehouse. "
            "Stores de-identified radiomic features, genomic predictions, "
            "and external multi-omic labels for Kenya/Saudi fine-tuning."
        )
        client.create_dataset(dataset)
        logger.info(f"[OK] Dataset '{dataset_id}' created.")
    return dataset_ref


def ensure_table(
    client: bigquery.Client,
    dataset_ref: bigquery.DatasetReference,
    table_name: str,
    schema: list,
    partition_field: str = None,
) -> None:
    table_ref = dataset_ref.table(table_name)
    try:
        existing = client.get_table(table_ref)
        # Schema evolution: add any missing columns
        existing_names = {f.name for f in existing.schema}
        new_fields = [f for f in schema if f.name not in existing_names]
        if new_fields:
            logger.info(f"[EVOLVE] Table '{table_name}': adding {len(new_fields)} new column(s): "
                        f"{[f.name for f in new_fields]}")
            updated_schema = list(existing.schema) + new_fields
            existing.schema = updated_schema
            client.update_table(existing, ["schema"])
            logger.info(f"[OK] Table '{table_name}' schema evolved.")
        else:
            logger.info(f"[OK] Table '{table_name}' already up-to-date.")
    except Exception:
        logger.info(f"[CREATE] Table '{table_name}' not found — creating...")
        table = bigquery.Table(table_ref, schema=schema)
        # Partition by ingested_at or created_at on tables that have it
        pf = partition_field or next(
            (f.name for f in schema if f.name in ("ingested_at", "created_at", "predicted_at",
                                                    "generated_at", "extracted_at", "deployed_at")),
            None
        )
        if pf:
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.MONTH,
                field=pf,
            )
        client.create_table(table)
        logger.info(f"[OK] Table '{table_name}' created.")


def seed_model_registry(client: bigquery.Client) -> None:
    """Pre-seed the model_registry with the trained Phase C1 models."""
    table_id = f"{PROJECT_ID}.{DATASET_ID}.model_registry"

    # Check if already seeded
    result = list(client.query(f"SELECT COUNT(*) as cnt FROM `{table_id}`").result())
    if result[0].cnt > 0:
        logger.info(f"[OK] model_registry already seeded ({result[0].cnt} rows).")
        return

    now = datetime.utcnow().isoformat()
    rows = [
        {
            "model_id":        "egfr_mlp_v1",
            "model_type":      "MLP",
            "target_gene":     "EGFR",
            "target_task":     "MUTATION_CLASSIFIER",
            "auc_val":         0.82,
            "auc_train":       0.91,
            "f1_score":        0.78,
            "training_cohort": "TCIA_NSCLC_211",
            "n_samples":       211,
            "feature_count":   148,
            "feature_names":   json.dumps(["shape_sphericity", "texture_entropy", "wavelet_LLH",
                                            "intensity_mean", "lesion_volume_mm3"]),
            "hyperparameters": json.dumps({"layers": [64, 32, 1], "dropout": 0.4,
                                            "lr": 0.001, "epochs": 50, "batch_size": 16}),
            "gcs_path":        "gs://vurarad-models/egfr_mlp_v1.pth",
            "deployed_at":     now,
            "status":          "ACTIVE",
            "notes":           "Phase C1 deep learning classifier. MLP with BatchNorm + Dropout.",
        },
        {
            "model_id":        "egfr_rf_v1",
            "model_type":      "RandomForest",
            "target_gene":     "EGFR",
            "target_task":     "MUTATION_CLASSIFIER",
            "auc_val":         0.79,
            "auc_train":       0.97,
            "f1_score":        0.74,
            "training_cohort": "TCIA_NSCLC_211",
            "n_samples":       211,
            "feature_count":   148,
            "feature_names":   json.dumps(["shape_sphericity", "texture_entropy", "wavelet_LLH",
                                            "intensity_mean", "lesion_volume_mm3"]),
            "hyperparameters": json.dumps({"n_estimators": 200, "max_depth": 10,
                                            "min_samples_split": 5, "class_weight": "balanced"}),
            "gcs_path":        "gs://vurarad-models/egfr_rf_v1.pkl",
            "deployed_at":     now,
            "status":          "ACTIVE",
            "notes":           "Phase C1 Scenario A — RandomForest baseline. High train AUC suggests mild overfit.",
        },
        {
            "model_id":        "kras_mlp_v1",
            "model_type":      "MLP",
            "target_gene":     "KRAS",
            "target_task":     "MUTATION_CLASSIFIER",
            "auc_val":         0.76,
            "auc_train":       0.88,
            "f1_score":        0.71,
            "training_cohort": "TCIA_NSCLC_211",
            "n_samples":       211,
            "feature_count":   148,
            "feature_names":   json.dumps(["texture_entropy", "shape_elongation",
                                            "intensity_kurtosis", "wavelet_HHH"]),
            "hyperparameters": json.dumps({"layers": [64, 32, 1], "dropout": 0.4,
                                            "lr": 0.001, "epochs": 50, "batch_size": 16}),
            "gcs_path":        "gs://vurarad-models/kras_mlp_v1.pth",
            "deployed_at":     now,
            "status":          "ACTIVE",
            "notes":           "Phase C1 KRAS classifier. Lower AUC expected due to KRAS heterogeneity.",
        },
    ]

    errors = client.insert_rows_json(table_id, rows)
    if errors:
        logger.error(f"[ERROR] Failed to seed model_registry: {errors}")
    else:
        logger.info(f"[OK] model_registry seeded with {len(rows)} models.")


def main():
    logger.info("=" * 65)
    logger.info("VuraRAD Radiogenomics Schema Provisioner v2.0")
    logger.info(f"  Project       : {PROJECT_ID}")
    logger.info(f"  Datasets      : {DATASET_ID}, {CORE_DATASET_ID}")
    logger.info(f"  Region        : {DATASET_REGION}")
    logger.info(f"  Improvements  : A,B,E,F,G,H (MPI, flat mutations, DSO seal,")
    logger.info(f"                  multi-lesion, provenance, model registry)")
    logger.info("=" * 65)

    client = bigquery.Client(project=PROJECT_ID)

    # ── Provision vura_core dataset + MPI ────────────────────────────────────
    logger.info("\n[PHASE 1] Core Dataset (vura_core)...")
    core_ref = ensure_dataset(client, CORE_DATASET_ID)
    for table_name, schema in CORE_TABLES.items():
        ensure_table(client, core_ref, table_name, schema)

    # ── Provision radiogenomics dataset + all tables ──────────────────────────
    logger.info("\n[PHASE 2] Radiogenomics Dataset...")
    radio_ref = ensure_dataset(client, DATASET_ID)
    for table_name, schema in RADIOGENOMICS_TABLES.items():
        ensure_table(client, radio_ref, table_name, schema)

    # ── Seed model_registry ────────────────────────────────────────────────────
    logger.info("\n[PHASE 3] Seeding Model Registry...")
    seed_model_registry(client)

    logger.info("")
    logger.info("=" * 65)
    logger.info("[DONE] All tables provisioned (schema evolution applied).")
    logger.info("Next step: run rebuild_view_and_extract.py to refresh training_master view.")
    logger.info("=" * 65)


if __name__ == "__main__":
    main()
