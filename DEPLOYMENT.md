# Inflation-Busting Recipe Generator - Deployment Guide

## Prerequisites
- Google Cloud Project with Dataflow enabled
- Docker installed locally
- `gcloud` CLI installed and configured

## Environment Setup

Create a `.env` file in the project root with:
```
GROQ_API_KEY=your_groq_api_key
SUPABASE_URL=your_supabase_url
SUPABASE_KEY=your_supabase_key
```

## Local Testing

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run locally:**
   ```bash
   streamlit run app.py
   ```

## Docker Deployment

1. **Build Docker image:**
   ```bash
   docker build -t inflation-recipe-generator:latest .
   ```

2. **Run Docker container:**
   ```bash
   docker run -p 8501:8501 \
     --env-file .env \
     inflation-recipe-generator:latest
   ```

## Deploy to Google Cloud Dataflow

1. **Set up Google Cloud Project:**
   ```bash
   gcloud config set project YOUR_PROJECT_ID
   gcloud auth configure-docker gcr.io
   ```

2. **Tag and push Docker image to Container Registry:**
   ```bash
   docker tag inflation-recipe-generator:latest \
     gcr.io/YOUR_PROJECT_ID/inflation-recipe-generator:latest
   
   docker push gcr.io/YOUR_PROJECT_ID/inflation-recipe-generator:latest
   ```

3. **Deploy to Cloud Run (alternative to Dataflow):**
   ```bash
   gcloud run deploy inflation-recipe-generator \
     --image gcr.io/YOUR_PROJECT_ID/inflation-recipe-generator:latest \
     --platform managed \
     --region us-central1 \
     --allow-unauthenticated \
     --set-env-vars GROQ_API_KEY=your_key,SUPABASE_URL=your_url,SUPABASE_KEY=your_key
   ```

## Project Structure

```
Inflation-Busting_Recipe_Generator/
├── app.py                 # Main Streamlit app
├── auth.py               # Authentication module
├── chatbot.py            # Chatbot logic
├── config.py             # Configuration & LLM setup
├── requirements.txt      # Python dependencies
├── Dockerfile            # Docker configuration
├── .dockerignore          # Docker build ignore rules
├── .gitignore            # Git ignore rules
├── db/
│   └── schema.sql        # Database schema
├── data_ingestion/
│   ├── fetch_open_food_facts.py
│   └── load_to_supabase.py
├── prompts/
│   └── recipe_prompt.md
└── utils/
    └── pdf_utils.py
```

## Troubleshooting

- **Module not found errors:** Ensure you're running from the project directory
- **Environment variables not loading:** Check `.env` file is in project root and contains all required keys
- **Streamlit port issues:** Port 8501 must be available or set `--server.port` to a different port

## Support

For issues, check the GitHub repository: https://github.com/SNishok-dbo/Inflation-Busting_Recipe_Generator
