from django.shortcuts import render, redirect
from django.contrib import messages
from .models import UploadedFile
from databricks.connect import DatabricksSession
from azure.storage.filedatalake import DataLakeServiceClient
from django.conf import settings
import hashlib
import logging
import requests
import time
import json
import pandas as pd
from io import StringIO
import os
import datetime  # Added for datetime.now()
from django.http import JsonResponse 
from urllib.parse import urlencode

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

def home(request):
    return render(request, 'home.html')

def log_analytics(request):
    if request.method == 'POST':
        uploaded_file = request.FILES.get('csv_file')

        if not uploaded_file:
            messages.error(request, "No file uploaded.")
            return render(request, 'log_analytics.html')

        valid_extensions = ('.csv', '.log', '.txt')
        if not uploaded_file.name.lower().endswith(valid_extensions):
            messages.error(request, "Only CSV, log, or text files are allowed.")
            return render(request, 'log_analytics.html')

        account_name = settings.AZURE_ACCOUNT_NAME
        account_key = settings.AZURE_ACCOUNT_KEY
        file_system = settings.AZURE_FILE_SYSTEM
        directory = "catalogiq"
        tracking_file = "uploaded_files.txt"

        try:
            service_client = DataLakeServiceClient(
                account_url=f"https://{account_name}.dfs.core.windows.net",
                credential=account_key
            )
            file_system_client = service_client.get_file_system_client(file_system)
            try:
                directory_client = file_system_client.get_directory_client(directory)
                directory_client.get_directory_properties()
            except:
                directory_client = file_system_client.create_directory(directory)
        except Exception as e:
            logger.error(f"ADLS Gen2 connection failed: {str(e)}")
            messages.error(request, f"Storage connection failed: {str(e)}")
            return render(request, 'log_analytics.html')

        # Check if file already exists in ADLS Gen2
        file_exists = False
        try:
            existing_file_client = directory_client.get_file_client(uploaded_file.name)
            existing_file_client.get_file_properties()
            file_exists = True
        except:
            file_exists = False

        if file_exists:
            messages.info(request, f"File '{uploaded_file.name}' already uploaded.")
            return render(request, 'log_analytics.html')

        # Save file locally
        temp_path = os.path.join(settings.TEMP_DIR, uploaded_file.name)
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        try:
            with open(temp_path, 'wb') as f:
                for chunk in uploaded_file.chunks():
                    f.write(chunk)
        except Exception as e:
            logger.error(f"Failed to save temp file: {str(e)}")
            messages.error(request, f"Temp file save failed: {str(e)}")
            return render(request, 'log_analytics.html')

        # Upload new file to ADLS Gen2
        try:
            file_client = directory_client.create_file(uploaded_file.name)
            with open(temp_path, 'rb') as data:
                file_contents = data.read()
                file_client.append_data(data=file_contents, offset=0, length=len(file_contents))
                file_client.flush_data(len(file_contents))
        except Exception as e:
            logger.error(f"Upload to ADLS failed: {str(e)}")
            messages.error(request, f"Upload failed: {str(e)}")
            return render(request, 'log_analytics.html')

        # Update uploaded_files.txt
        try:
            tracking_client = directory_client.create_file(tracking_file)
            tracking_client.append_data(data=(uploaded_file.name + "\n").encode(), offset=0, length=len(uploaded_file.name) + 1)
            tracking_client.flush_data(len(uploaded_file.name) + 1)
        except Exception as e:
            logger.warning(f"Failed to update tracking file: {str(e)}")
            # Don't block upload on this

        # Update model
        try:
            uploaded_file_obj, created = UploadedFile.objects.get_or_create(filename=uploaded_file.name)
            uploaded_file_obj.created_at = datetime.datetime.now()
            uploaded_file_obj.analyzed = False
            uploaded_file_obj.output_path = None
            uploaded_file_obj.save()
        except Exception as e:
            logger.error(f"Failed to save metadata: {str(e)}")

        # Clean up
        if os.path.exists(temp_path):
            os.remove(temp_path)

        messages.success(request, f"File '{uploaded_file.name}' uploaded successfully to ADLS Gen2!")
        return redirect('log_analytics')

    return render(request, 'log_analytics.html')
def analysis_options(request):
    logger.debug("Rendering analysis_options.html")
    return render(request, 'analysis_options.html')

def anomaly_detection(request):
    logger.debug("Handling anomaly_detection request")
    
    # Azure configuration
    account_name = settings.AZURE_ACCOUNT_NAME
    account_key = settings.AZURE_ACCOUNT_KEY
    output_file_system = "anomaliesdet"
    output_directory = "log_analysis_output"

    # Get the latest uploaded file
    latest_file = UploadedFile.objects.order_by('-created_at').first()
    if not latest_file:
        messages.info(request, "No files uploaded yet.")
        return render(request, 'anomaly_detection.html', {'results': []})

    # Construct output path based on input file name
    base_filename = os.path.splitext(latest_file.filename)[0]
    output_filename = f"{base_filename}.txt"
    output_path = f"{output_directory}/{output_filename}"

    # Initialize ADLS client
    try:
        service_client = DataLakeServiceClient(
            account_url=f"https://{account_name}.dfs.core.windows.net",
            credential=account_key
        )
        file_system_client = service_client.get_file_system_client(output_file_system)
        file_client = file_system_client.get_file_client(output_path)
    except Exception as e:
        logger.error(f"Failed to initialize ADLS client: {str(e)}")
        messages.error(request, "Failed to connect to storage.")
        return render(request, 'anomaly_detection.html', {'results': []})

    # Check for existing output in ADLS
    results = []
    try:
        file_client.get_file_properties()
        logger.debug(f"Output found in ADLS: {output_path}")
        download = file_client.download_file()
        file_content = download.readall().decode('utf-8')
        df = pd.read_csv(StringIO(file_content))
        results = df.to_dict('records')
        logger.debug("Successfully retrieved existing output")
        if not latest_file.analyzed:
            latest_file.analyzed = True
            latest_file.output_path = output_path
            latest_file.save()
            logger.debug(f"Updated metadata for {latest_file.filename}")
    except Exception as e:
        logger.debug(f"No existing output found in ADLS: {output_path}")

        if latest_file.analyzed:
            messages.info(request, "No analysis results available for this file.")
            return render(request, 'anomaly_detection.html', {'results': []})

        notebook_path = "/Users/nandakrishna@sateeshaimlgmail.onmicrosoft.com/log analysis"
        try:
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            job_data = {
                "name": f"Anomaly_Detection_{latest_file.filename}",
                "existing_cluster_id": settings.DATABRICKS_CLUSTER_ID,
                "notebook_task": {
                    "notebook_path": notebook_path,
                    "base_parameters": {
                        "input_path": f"abfss://rawfiles@{account_name}.dfs.core.windows.net/catalogiq/{latest_file.filename}",
                        "output_path": f"abfss://{output_file_system}@{account_name}.dfs.core.windows.net/{output_path}"
                    }
                }
            }
            create_response = requests.post(
                f"{settings.DATABRICKS_HOST}/api/2.1/jobs/create",
                headers=headers,
                json=job_data
            )
            if create_response.status_code != 200:
                logger.error(f"Failed to create Databricks job: {create_response.text}")
                messages.error(request, f"Failed to configure analysis: {create_response.json().get('message', 'Unknown error')}")
                return render(request, 'anomaly_detection.html', {'results': []})
            job_id = create_response.json().get("job_id")
            logger.debug(f"Created Databricks job: job_id={job_id}")

            run_response = requests.post(
                f"{settings.DATABRICKS_HOST}/api/2.1/jobs/run-now",
                headers=headers,
                json={"job_id": job_id}
            )
            if run_response.status_code != 200:
                logger.error(f"Failed to run Databricks job: {run_response.text}")
                messages.error(request, f"Failed to run analysis: {run_response.json().get('message', 'Unknown error')}")
                return render(request, 'anomaly_detection.html', {'results': []})
            run_id = run_response.json().get("run_id")
            logger.debug(f"Triggered Databricks job: run_id={run_id}")

            max_attempts = 30
            attempt = 0
            while attempt < max_attempts:
                response = requests.get(
                    f"{settings.DATABRICKS_HOST}/api/2.1/jobs/runs/get?run_id={run_id}",
                    headers=headers
                )
                if response.status_code != 200:
                    logger.error(f"Failed to check job status: {response.text}")
                    messages.error(request, "Failed to monitor analysis progress.")
                    return render(request, 'anomaly_detection.html', {'results': []})
                run_status = response.json().get("state", {}).get("life_cycle_state")
                if run_status in ["TERMINATED", "SKIPPED", "INTERNAL_ERROR"]:
                    result_state = response.json().get("state", {}).get("result_state")
                    state_message = response.json().get("state", {}).get("state_message", "No details provided")
                    if result_state == "SUCCESS":
                        logger.debug("Databricks job completed successfully")
                        try:
                            file_client.get_file_properties()
                            download = file_client.download_file()
                            file_content = download.readall().decode('utf-8')
                            df = pd.read_csv(StringIO(file_content))
                            results = df.to_dict('records')
                            latest_file.analyzed = True
                            latest_file.output_path = output_path
                            latest_file.save()
                            logger.debug(f"Updated metadata for {latest_file.filename}")
                        except Exception as output_error:
                            logger.error(f"Output file not found after job completion: {str(output_error)}")
                            messages.error(request, f"Analysis completed but output file not found: {str(output_error)}")
                            return render(request, 'anomaly_detection.html', {'results': []})
                        break
                    else:
                        logger.error(f"Databricks job failed: {result_state} - {state_message}")
                        messages.error(request, f"Analysis failed: {state_message}")
                        return render(request, 'anomaly_detection.html', {'results': []})
                time.sleep(10)
                attempt += 1
            else:
                logger.error("Databricks job timed out")
                messages.error(request, "Analysis timed out. Please try again later.")
                return render(request, 'anomaly_detection.html', {'results': []})
        except Exception as e:
            logger.error(f"Failed to trigger Databricks job: {str(e)}")
            messages.error(request, f"Failed to run analysis: {str(e)}")
            return render(request, 'anomaly_detection.html', {'results': []})

    return render(request, 'anomaly_detection.html', {'results': results})

def stream_viewer(request):
    logger.debug("Handling stream_viewer request")
    
    # Azure configuration
    account_name = settings.AZURE_ACCOUNT_NAME
    silver_file_system = "silver"

    # Get the latest uploaded file
    latest_file = UploadedFile.objects.order_by('-created_at').first()
    if not latest_file:
        messages.info(request, "No files uploaded yet.")
        return render(request, 'stream_viewer.html', {'results': [], 'columns': [], 'filter_values': {}})

    # Construct Parquet path in silver container
    base_filename = os.path.splitext(latest_file.filename)[0]
    parquet_path = f"abfss://{silver_file_system}@{account_name}.dfs.core.windows.net/{base_filename}/"

    # Read Parquet file using Databricks Connect
    try:
        spark = DatabricksSession.builder.remote(
            host=settings.DATABRICKS_HOST,
            token=settings.DATABRICKS_TOKEN,
            cluster_id=settings.DATABRICKS_CLUSTER_ID
        ).getOrCreate()
        df = spark.read.parquet(parquet_path)
        pandas_df = df.toPandas()
        results = pandas_df.to_dict('records')
        columns = pandas_df.columns.tolist()
        
        # Prepare filter values for each column
        filter_values = {}
        for col in columns:
            unique_values = pandas_df[col].dropna().unique().tolist()
            filter_values[col] = [str(val) for val in unique_values]
        logger.debug(f"Successfully read Parquet file from {parquet_path}")
    except Exception as e:
        logger.error(f"Failed to read Parquet file: {str(e)}")
        messages.error(request, f"Failed to load stream data: {str(e)}")
        return render(request, 'stream_viewer.html', {'results': [], 'columns': [], 'filter_values': {}})

    return render(request, 'stream_viewer.html', {
        'results': results,
        'columns': columns,
        'filter_values': filter_values
    })
    
    # In catalog/views.py, add this new view at the end

def chatbot(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            query = data.get('query', '')

            if not query:
                return JsonResponse({'response': 'Please provide a query.'}, status=400)

            # Call Databricks serving endpoint
            endpoint_url = "https://adb-3623933893880845.5.azuredatabricks.net/serving-endpoints/andriodlog/invocations"
            headers = {
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {
                "inputs": [query]
            }

            response = requests.post(endpoint_url, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                # Adjust based on your endpoint's response structure
                bot_response = result.get('predictions', ['Sorry, I could not process that.'])[0]
            else:
                logger.error(f"Databricks endpoint error: {response.text}")
                bot_response = f"Error from service: {response.json().get('message', 'Unknown error')}"

            return JsonResponse({'response': bot_response})
        except Exception as e:
            logger.error(f"Chatbot error: {str(e)}")
            return JsonResponse({'response': f"Error: {str(e)}"}, status=500)

    return render(request, 'chatbot.html')

# views.py
import requests
from django.conf import settings
from django.http import HttpResponse, HttpResponseServerError
import logging


def databricks_dashboard_proxy(request):
    # Construct the embed URL
    dashboard_url = f"{settings.DATABRICKS_WORKSPACE_URL}/embed/dashboardsv3/{settings.DATABRICKS_DASHBOARD_ID}"

    # Add any query parameters from the frontend request if needed
    params = request.GET.dict()

    try:
        # Call Databricks dashboard with the PAT in Authorization header
        response = requests.get(
            dashboard_url,
            headers={
                "Authorization": f"Bearer {settings.DATABRICKS_TOKEN}",
                "Accept": "text/html",
            },
            params=params,
            timeout=10
        )

        if response.status_code == 200:
            # Return the dashboard HTML to frontend iframe
            return HttpResponse(response.content, content_type='text/html')
        else:
            logger.error(f"Databricks dashboard fetch failed: {response.status_code} {response.text}")
            return HttpResponseServerError("Failed to fetch dashboard")

    except Exception as e:
        logger.error(f"Error fetching dashboard: {e}")
        return HttpResponseServerError("Internal Server Error")