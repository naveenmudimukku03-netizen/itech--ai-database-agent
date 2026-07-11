# 🤖 AI Database Agent

An AI-powered Database Agent built using **FastAPI**, **OpenRouter LLM**, **Natural Language Processing**, and **Data Visualization** that enables users to interact with databases using plain English.

Instead of writing SQL queries manually, users can ask questions in natural language, and the AI automatically generates SQL queries, executes them, and displays the results as tables or interactive charts.

---

## 🚀 Features

- 💬 Natural Language to SQL Conversion
- 🤖 AI-powered Query Generation using OpenRouter LLM
- 📊 Automatic Chart Generation
- 📈 Dashboard Analytics
- 📜 Query History
- 💾 Session History
- 📤 Export Results to CSV
- 📥 Export Results to Excel
- 🔍 Database Schema Detection
- 📊 Multiple Chart Types
- ⚡ FastAPI Backend
- 🌐 Responsive Web Interface
- ☁️ Vercel Deployment Support
- 🔒 Secure API Configuration

---

# Tech Stack

## Frontend

- HTML5
- CSS3
- JavaScript
- Bootstrap

## Backend

- FastAPI
- Python

## AI Model

- OpenRouter API
- Gemini / GPT Models

## Database

- SQLite (Local Development)
- Supabase PostgreSQL (Production)

## Data Processing

- Pandas
- NumPy

## Charts

- Plotly
- Chart.js

---

# Project Structure

```
AI-Database-Agent/
│
├── app.py
├── requirements.txt
├── demo.db
├── static/
├── templates/
├── assets/
├── README.md
├── .env
└── vercel.json
```

---

# Installation

## Clone Repository

```bash
git clone https://github.com/yourusername/AI-Database-Agent.git

cd AI-Database-Agent
```

---

## Create Virtual Environment

Windows

```bash
python -m venv venv
```

Activate

```bash
venv\Scripts\activate
```

---

## Install Dependencies

```bash
pip install -r requirements.txt
```

---

# Environment Variables

Create a `.env` file.

```env
OPENAI_API_KEY=YOUR_OPENROUTER_API_KEY
OPENAI_MODEL=google/gemma-3-27b-it
OPENAI_API_BASE=https://openrouter.ai/api/v1

DATABASE_URL=postgresql://postgres:PASSWORD@db.xxxxx.supabase.co:5432/postgres
```

---

# Run Application

```bash
uvicorn app:app --reload
```

Application runs at

```
http://127.0.0.1:8000
```

---

# API Endpoints

| Method | Endpoint | Description |
|----------|----------------|-------------------------|
| GET | / | Home Page |
| GET | /api/config | Get Configuration |
| POST | /api/chat | Generate SQL Query |
| GET | /api/history | Chat History |
| GET | /api/dashboard | Dashboard Analytics |
| GET | /api/export/csv | Export CSV |
| GET | /api/export/excel | Export Excel |

---

# Workflow

```
User
   │
   ▼
Natural Language Question
   │
   ▼
FastAPI Backend
   │
   ▼
OpenRouter LLM
   │
   ▼
SQL Query Generation
   │
   ▼
Database Execution
   │
   ▼
Results
   │
   ├────────────► Table
   │
   └────────────► Charts
```

---

# Example Queries

```
Show total sales by region

Top 10 customers

Monthly revenue

Revenue by product category

Orders placed last month

Average sales per customer

Top selling products

Region wise profit

Yearly sales report

Customer count by region
```

---

# Supported Charts

- Bar Chart
- Line Chart
- Pie Chart
- Area Chart
- Scatter Plot
- Horizontal Bar Chart
- Doughnut Chart

---

# Deployment

## Local

```bash
uvicorn app:app --reload
```

## Vercel

```bash
vercel --prod
```

## Production Database

Supabase PostgreSQL

---

# Future Enhancements

- Voice-based Queries
- Role-based Authentication
- Multi-Database Support
- AI Query Suggestions
- Scheduled Reports
- Dark Mode
- PDF Export
- Real-time Dashboard
- Database Connection Wizard

---

# Advantages

- No SQL knowledge required
- Faster report generation
- Interactive visualizations
- AI-assisted analytics
- Easy deployment
- User-friendly interface

---

# Use Cases

- Business Analytics
- Sales Reporting
- Data Exploration
- Educational Projects
- Database Learning
- Dashboard Automation
- Management Reporting

---

# Requirements

Python 3.11+

FastAPI

Uvicorn

Pandas

Plotly

OpenAI

python-dotenv

psycopg2-binary

---

# Author

**Naveen Kumar**

B.E Computer Science and Engineering (AI & ML)

Aarupadai Veedu Institute of Technology (AVIT)

---

# License

This project is developed for educational, research, and demonstration purposes.
