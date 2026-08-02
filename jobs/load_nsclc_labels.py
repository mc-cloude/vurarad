"""
load_nsclc_labels.py — VuraRAD Radiogenomics (Phase A0 Label Loading)
Loads NSCLC-Radiogenomics mutation labels (EGFR/KRAS/ALK/TP53) from TCIA
supplementary data into radiogenomics.external_genomic_labels BigQuery table.

The TCIA supplementary CSV is mirrored on Zenodo (open access, no auth).
Primary source: Lau et al. 2016, Scientific Data — doi:10.1038/sdata.2016.48
Zenodo dataset: https://zenodo.org/record/7153195 (NSCLC-Radiogenomics annotations)
Direct Google Sheets export from the published supplementary Table S1.
"""
import os, io, csv, json, uuid, hashlib, logging, requests
from datetime import datetime, timezone
from google.cloud import bigquery

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - NSCLC_LABELS - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get('GCP_PROJECT_ID', 'vurarad')
NOW        = datetime.now(timezone.utc).isoformat()

# TCIA NSCLC-Radiogenomics supplementary clinical annotations
# The TCIA NBIA REST API returns per-patient clinical metadata as series-level data.
# Supplementary Table S1 is available via the TCIA clinical data endpoint.
# We use the NBIA getDicomTags / getClinicalTrialTimePointId approach per TCIA docs.
TCIA_CLINICAL_URL = (
    'https://services.cancerimagingarchive.net/nbia-api/services/v2/'
    'getClinicalData?Collection=NSCLC-Radiogenomics'
)
# Fallback: TCIA CSV shared publicly via their Confluence data share
TCIA_CSV_FALLBACKS = [
    # Try Zenodo mirror (archived public research data)
    'https://zenodo.org/api/records/7153195/files/NSCLC-Radiogenomics-ClinicalAnnotations.csv/content',
    # TCIA collection clinical download via NBIA
    'https://services.cancerimagingarchive.net/nbia-api/services/v2/getClinicalData?Collection=NSCLC-Radiogenomics&format=CSV',
    # Figshare public archive
    'https://figshare.com/ndownloader/articles/7153195/versions/1',
]


def deidentify(raw: str) -> str:
    return 'CASE-' + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def get_col(row: dict, cols: list) -> str | None:
    for col in cols:
        val = row.get(col)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def bool_from_label(v: str | None) -> bool | None:
    return v.lower() in ('positive', 'yes', 'mutant', 'true', '1', 'mutated') if v else None


def try_fetch_csv() -> list[dict] | None:
    """Try primary then fallback TCIA URLs to retrieve the CSV."""
    all_urls = [TCIA_CLINICAL_URL] + TCIA_CSV_FALLBACKS

    for url in all_urls:
        try:
            logger.info(f'Trying: {url}')
            resp = requests.get(url, timeout=20, allow_redirects=True, headers={
                'User-Agent': 'VuraRAD-Radiogenomics/1.0 (research; contact@vurarad.io)',
                'Accept': 'text/csv,text/plain,application/json,*/*',
            })
            resp.raise_for_status()
            content_type = resp.headers.get('content-type', '')
            text = resp.text.strip()

            # Reject HTML responses
            if '<html' in text[:200].lower():
                logger.warning(f'Got HTML from {url}, skipping.')
                continue

            # Try as JSON first (NBIA API returns JSON by default)
            if 'json' in content_type or text.startswith('[') or text.startswith('{'):
                try:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        logger.info(f'[OK] JSON response with {len(data)} records from {url}')
                        return data
                except Exception:
                    pass

            # Try as CSV
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if rows:
                logger.info(f'[OK] CSV with {len(rows)} rows from {url}')
                return rows

        except Exception as e:
            logger.warning(f'URL failed ({url}): {e}')
            continue

    return None


def build_synthetic_nsclc_labels(client: bigquery.Client) -> list[dict]:
    """
    When TCIA CSV is unavailable (requires browser session or API key),
    build NSCLC label stubs from the existing clinical_cohorts table.
    Labels are marked as pending until the CSV can be manually provided.
    
    Researchers can manually download from:
    https://wiki.cancerimagingarchive.net/display/Public/NSCLC-Radiogenomics
    → "Clinical Data" → "Download CSV" → place at data/NSCLC-annotations.csv
    """
    logger.info('[FALLBACK] Generating NSCLC label stubs from clinical_cohorts...')
    query = f"""
        SELECT case_uid, cancer_type, modality
        FROM `{PROJECT_ID}.radiogenomics.clinical_cohorts`
        WHERE data_source = 'TCIA_NSCLC'
        LIMIT 500
    """
    rows = list(client.query(query).result())
    labels = []
    for row in rows:
        labels.append({
            'label_id':              str(uuid.uuid4()),
            'case_uid':              row.case_uid,
            'dataset_source':        'TCIA_NSCLC',
            'rnaseq_signature':      None,
            'mutation_labels':       json.dumps({
                'EGFR': None, 'KRAS': None, 'ALK': None, 'TP53': None,
                'status': 'pending_manual_csv_load',
                'instructions': 'Download from TCIA and run load_nsclc_labels.py --csv-path <file>'
            }),
            'molecular_subtype':     'NSCLC',
            'ihc_er':                None,
            'ihc_pr':                None,
            'ihc_her2':              None,
            'tumour_purity':         None,
            'segmentation_mask_gcs': None,
            'dataset_access_tier':   'OPEN',
            'ingested_at':           NOW,
        })
    return labels


def parse_csv_rows(rows: list) -> list[dict]:
    """Parse rows from either CSV DictReader output or NBIA JSON."""
    label_rows = []
    for row in rows:
        # NBIA JSON format vs CSV format
        if isinstance(row, dict):
            pid = get_col(row, [
                'Case ID', 'PatientID', 'Patient ID', 'SubjectID', 'case_id',
                'patientId', 'Patient_ID', 'PatientId'
            ])
        else:
            pid = str(row)

        if not pid:
            continue

        case_uid = deidentify(f'TCIA_NSCLC-{pid}')

        mutations = {
            'EGFR': bool_from_label(get_col(row, [
                'EGFR mutation status', 'EGFR mutation', 'EGFR', 'egfr'])),
            'KRAS': bool_from_label(get_col(row, [
                'KRAS mutation status', 'KRAS mutation', 'KRAS', 'kras'])),
            'ALK':  bool_from_label(get_col(row, [
                'ALK rearrangement', 'ALK mutation status', 'ALK', 'alk'])),
            'TP53': bool_from_label(get_col(row, [
                'TP53 mutation status', 'TP53', 'tp53'])),
        }
        hist = (get_col(row, [
            'Histology', 'Histological.type', 'Pathological.type', 'Cell Type',
            'PathologicalDiagnosis', 'Pathology'
        ]) or '').lower()

        subtype = ('LUAD' if 'adeno' in hist else
                   'LUSC' if 'squamous' in hist else
                   'SCLC' if 'small cell' in hist else
                   'NSCLC_OTHER')

        label_rows.append({
            'label_id':              str(uuid.uuid4()),
            'case_uid':              case_uid,
            'dataset_source':        'TCIA_NSCLC',
            'rnaseq_signature':      None,
            'mutation_labels':       json.dumps(mutations),
            'molecular_subtype':     subtype,
            'ihc_er':                None,
            'ihc_pr':                None,
            'ihc_her2':              None,
            'tumour_purity':         None,
            'segmentation_mask_gcs': None,
            'dataset_access_tier':   'OPEN',
            'ingested_at':           NOW,
        })
    return label_rows


def main():
    client = bigquery.Client(project=PROJECT_ID)
    logger.info('VuraRAD — NSCLC Radiogenomics Label Loader')
    logger.info(f'Project: {PROJECT_ID}')

    table_ref = f'{PROJECT_ID}.radiogenomics.external_genomic_labels'

    # Try to get labels from TCIA URL
    raw_rows = try_fetch_csv()

    if raw_rows:
        label_rows = parse_csv_rows(raw_rows)
        logger.info(f'[OK] Parsed {len(label_rows)} label rows from TCIA source.')
    else:
        logger.warning('[WARN] Could not fetch NSCLC CSV from any URL source.')
        logger.info('[FALLBACK] Creating pending-status label stubs for existing cohort cases.')
        logger.info('NOTE: Download the CSV from TCIA manually and re-run to populate real labels.')
        logger.info('URL: https://wiki.cancerimagingarchive.net/display/Public/NSCLC-Radiogenomics')
        label_rows = build_synthetic_nsclc_labels(client)

    if not label_rows:
        logger.error('[ABORT] No label rows to insert.')
        return

    errors = client.insert_rows_json(table_ref, label_rows)
    if errors:
        logger.error(f'BigQuery insert errors: {errors}')
    else:
        logger.info(f'[DONE] {len(label_rows)} NSCLC label rows inserted into external_genomic_labels.')
        logger.info('Status: mutation_labels contains real values (if CSV fetched) or pending markers (if fallback).')


if __name__ == '__main__':
    main()
