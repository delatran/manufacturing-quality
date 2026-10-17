## PAGE 1
Data Analyst — Case Study
Time: 3 business days
Submission: GitHub repository (public)
Getting Started
Clone the repository to get the master data and access the API endpoint:
git clone https://github.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026.git
or using GitHub CLI:
gh repo clone phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026
Context
You are about to join the Digital Innovation team as a Data Analyst. The team works with operations
teams to turn raw operational data into insights and digital tools they use every day: analyses, dashboards,
web applications and automated workflows.
Your first assignment comes from the Production team of a manufacturing plant. The plant operates 4
production lines (Line 1–4), producing 100 products across 4 categories.
The Production team currently manages defect data from 2 sources:
Master Data (product information)
Defect Records (defects logged from the production floor)
Business Problem
The plant records product defect data from 4 production lines. However, the data is scattered across
multiple sources and has not been used effectively.
Production Team Leaders & Managers currently need:
A consolidated view of production quality trends over time
Visibility into which products and lines have high defect rates
Understanding of how repair costs are distributed
Early detection of defect anomalies instead of waiting for manual weekly reports
The ability to export or share data with relevant departments when needed
Defect data consolidation and reporting is done manually today, taking 2–3 days. By the time the data
reaches decision-makers, it is already outdated.

## PAGE 2
Data Sources
1. Master Data — master_data.csv (in the repository)
Product reference data.
Field Type Description
product_id integer Unique identifier for each product
product_name string Name of the product
category string Product category (e.g., Category 1, Category 2)
production_line string Production line the product belongs to (e.g., Line-1, Line-2)
monthly_output integer Total monthly production output
2. Defect Records — API Endpoint
Defect data logged from the production floor.
Endpoint:
GET https://raw.githubusercontent.com/phongtdt/RFID_Data-Analyst_Case-Study-Oct-2026/main/mock-
api/defect_records.json
Response format: JSON array
Field Type Description
defect_id integer Unique identifier for each defect
product_id integer Identifier for the product associated with the defect
defect_type string Category of the defect (e.g., Cosmetic, Functional, Structural)
defect_date string Date when the defect was detected
defect_locationstring Location within the product where the defect was found (e.g., Surface,
Component)
severity string Severity level of the defect (e.g., Minor, Moderate, Critical)
inspection_methodstring Method used to detect the defect (e.g., Visual Inspection, Automated Testing)
repair_cost float Cost incurred to repair the defect (in local currency)
⚠  Defect records must be fetched via HTTP request. Do not download the JSON file and read it
locally.

## PAGE 3
Your Tasks
Part 1 — Data Preparation & Quality
Fetch and prepare the data for analysis. Document every data quality issue you find, and how you handled
it, using the Excel template DATA_QUALITY_TEMPLATE.xlsx provided in the repository. Save the completed
file as DATA_QUALITY.xlsx in your own repository.
Part 2 — Web App Dashboard
Design and build a web app dashboard that addresses the business problem above and communicates
insights from the data.
Backend — Python (FastAPI preferred): fetches both data sources, cleans the data, and calculates
all metrics and aggregations. Exposes the results to the frontend through API endpoints.
Frontend — any technology (React + TypeScript preferred): only calls the backend endpoints
and displays the results. Do not fetch the source data, clean it, or aggregate it in the frontend.
The solution must run, solve the business problem, and clearly communicate insights.
Part 3 — Analytical Questions
1. Trend Analysis: Analyse the number of defects from January to June 2024. What patterns do you
observe? Is this a positive or negative sign for the plant? Support your answer with numbers.
2. Defect Location & Severity: Where do defects occur most frequently (Component, Internal,
Surface)? Is there a relationship between defect location and severity level? What does this mean
for where the Production team should focus?
3. Pattern Recognition: Identify and present any other notable patterns you discover in the data. For
each pattern, explain why it matters to the Production team.
Part 4 — Recommendations
1. Priorities: Based on your analysis, what are the top 3 issues the Production team should address
first? For each issue, include:
The evidence from the data
The action you recommend
Who should own it
How success should be measured
2. Next steps for the tool: Data only delivers real value when it solves operational bottlenecks. If you
were an end user using this solution every day, what are the limitations of a "static dashboard", and
what capabilities would you add next?
Deliverables
Source code on GitHub, including:

## PAGE 4
README.md containing:
Setup and run instructions
A brief explanation of the dashboard's key components and features
Answers to the analytical questions (Part 3)
Your recommendations (Part 4)
DATA_QUALITY.xlsx completed from DATA_QUALITY_TEMPLATE.xlsx
Notes
You may use AI-assisted tools if needed (human-in-the-loop approach). Be ready to explain every
part of your solution.
Where the data or requirements are unclear, state your assumptions.
Submission deadline: 3 business days

## Current owner UI choice
The owner requests Dashboard and Agent tabs, with Dashboard as the default. Both tabs share datasets, KPI definitions, analytical filters and execution evidence. This UI choice is an owner instruction separate from the original PDF requirements above. All analytical tasks, workbook evidence and numerical provenance remain required. The PDF contains no numerical answer key.
