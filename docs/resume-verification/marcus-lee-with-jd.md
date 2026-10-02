# marcus-lee.pdf — version 6 — with a job description
status: **success**

<details><summary>Job description used</summary>

Senior Product Designer - Northwind Health

Northwind builds patient-facing mobile and web products used by millions of people to manage their care.

What you'll do
- Own end-to-end design for core patient flows (booking, messaging, prescriptions), from research to shipped UI
- Run user research and usability testing and turn findings into design decisions
- Build and maintain our design system in Figma and partner with engineers on implementation
- Define success metrics with product managers and show impact through experiments
- Mentor designers and raise the quality bar

What we're looking for
- 6+ years in product or UX design with a strong mobile portfolio
- Expert Figma and prototyping skills; comfortable with HTML/CSS
- Experience with accessibility (WCAG) and design systems at scale
- A track record of measurable improvements to conversion or task completion


</details>

## ATS Compatibility Agent — success
_groq / openai/gpt-oss-20b, 3852 ms, 1 reply(ies)_
```json
{
  "score": 35,
  "issues": [
    {
      "fix": "Reformat the resume into a single column layout.",
      "title": "Multi-column layout",
      "source": "agent",
      "category": "multi_column",
      "evidence": "page 1: 2 text column(s) (10 side-by-side block pairs)",
      "severity": "blocker",
      "explanation": "ATS reads columns left-to-right, so content may be scrambled and important details can be lost."
    },
    {
      "fix": "Move contact details into the main body of the resume.",
      "title": "Contact details in header/footer",
      "source": "agent",
      "category": "header_footer",
      "evidence": "text in the page header zone: 'marcus.lee@example.com | +1 415 555 0147 | Austin, TX'",
      "severity": "blocker",
      "explanation": "ATS often ignores header/footer, so email and phone may be omitted from the parsed data."
    },
    {
      "fix": "Replace icons with plain text.",
      "title": "Images or icons used for contact details",
      "source": "agent",
      "category": "image_or_icon",
      "evidence": "2 image(s) (2 icon-sized)",
      "severity": "warning",
      "explanation": "Images are not parsed as text, so contact info may be missing or incomplete."
    },
    {
      "fix": "Replace the table with plain text or separate sections.",
      "title": "Table used for layout",
      "source": "agent",
      "category": "table",
      "evidence": "1 table(s): 3x2",
      "severity": "warning",
      "explanation": "Tables can confuse ATS and merge cells, leading to misparsed information."
    },
    {
      "fix": "Rename sections to standard titles: Summary, Experience, Education, Skills.",
      "title": "Non-standard section headers",
      "source": "agent",
      "category": "nonstandard_header",
      "evidence": "Section headers found: TOOLBOX, HTML / CSS, LANGUAGES, ABOUT ME, MY JOURNEY, WHERE I STUDIED, SKILL LEVELS",
      "severity": "warning",
      "explanation": "ATS expects standard headers like Experience, Education, Skills; non-standard names may be ignored."
    },
    {
      "fix": "Replace 'Now' with 'Present' or a specific year.",
      "title": "Ambiguous date format",
      "source": "agent",
      "category": "date_format",
      "evidence": "experience dates: '2020 - Now'",
      "severity": "warning",
      "explanation": "Using 'Now' can confuse ATS; it may not recognize the end date."
    },
    {
      "fix": "Remove separators and list contact info in plain text.",
      "title": "Decorative separators in contact details",
      "source": "agent",
      "category": "special_characters",
      "evidence": "header contact: 'marcus.lee@example.com   |   +1 415 555 0147   |   Austin, TX'",
      "severity": "warning",
      "explanation": "Special characters like '|' can be misinterpreted or dropped by ATS."
    }
  ],
  "summary": "This resume has several layout and formatting issues that can cause ATS to misinterpret or lose key information. The main problems are multi-column text, header/footer contact details, and use of images and tables."
}
```

## Parser Agent — success
_gemini / gemini-3.5-flash-lite, 2773 ms, 1 reply(ies)_
```json
{
  "skills": [
    "Figma",
    "Sketch",
    "Adobe XD",
    "Prototyping",
    "User research",
    "HTML / CSS",
    "Design systems",
    "English",
    "Spanish"
  ],
  "summary": "Creative designer who loves solving problems and making things beautiful.\nAlways learning, always curious. Coffee enthusiast.",
  "education": [
    {
      "dates": "2012 - 2016",
      "degree": "BFA Graphic Design",
      "details": "",
      "institution": "University of Texas at Austin"
    }
  ],
  "experience": [
    {
      "dates": "2020 - Now",
      "title": "Lead Product Designer",
      "bullets": [
        "Responsible for the design of the patient mobile app",
        "Redesigned the appointment booking flow, which improved completion by 23%",
        "Worked with developers and product managers on many features",
        "Created a design system used by 3 product teams",
        "Ran user interviews"
      ],
      "company": "Lumen Health"
    },
    {
      "dates": "2016 - 2020",
      "title": "UX Designer",
      "bullets": [
        "Designed websites and apps for various clients",
        "Was in charge of the agency's usability testing process",
        "Delivered 40+ projects for startups and retailers",
        "Collaborated across teams to make great products"
      ],
      "company": "Brightpath Agency"
    }
  ],
  "contact_info": {
    "name": "Marcus Lee",
    "email": "marcus.lee@example.com",
    "links": [],
    "phone": "+1 415 555 0147",
    "location": "Austin, TX"
  }
}
```

## Content & Impact Agent — success
_gemini / gemini-3.5-flash-lite, 7252 ms, 2 reply(ies)_
_warnings: ["e1.b0 belongs to a PAST role: its rewrite must open with a past-tense verb (Developed, Led), not 'Design'", "e1.b3 belongs to a PAST role: its rewrite must open with a past-tense verb (Developed, Led), not 'Partner'"]_
_reply 1 rejected: ['~e0.b0 is flagged no_metric but its rewrite has no number and no [placeholder] such as [X%] or [N users]', '~e0.b2 is flagged no_metric but its rewrite has no number and no [placeholder] such as [X%] or [N users]', '~e0.b4 is flagged no_metric but its rewrite has no number and no [placeholder] such as [X%] or [N users]', "e1.b0's rewrite is identical to the original; change the wording, don't just move the punctuation", '~e1.b0 is flagged no_metric but its rewrite has no number and no [placeholder] such as [X%] or [N users]', '~e1.b3 is flagged no_metric but its rewrite has no number and no [placeholder] such as [X%] or [N users]']_
_reply 2 rejected: ["~e1.b0 belongs to a PAST role: its rewrite must open with a past-tense verb (Developed, Led), not 'Design'", "~e1.b3 belongs to a PAST role: its rewrite must open with a past-tense verb (Developed, Led), not 'Partner'"]_
```json
{
  "bullets": [
    {
      "id": "e0.b0",
      "flags": [
        "weak_opening_verb",
        "no_metric"
      ],
      "rewrite": "Design the patient mobile app for [N] users across [N] platforms",
      "explanation": "Starts with the weak phrase \"Responsible for\" and lacks any scope metric like team size or user base."
    },
    {
      "id": "e0.b1",
      "flags": [],
      "rewrite": "",
      "explanation": ""
    },
    {
      "id": "e0.b2",
      "flags": [
        "weak_opening_verb",
        "no_metric",
        "vague_claim"
      ],
      "rewrite": "Collaborate with [N] developers and product managers on [N] features",
      "explanation": "Starts with \"Worked on\" and uses the vague phrase \"many features\" without quantifying the scope."
    },
    {
      "id": "e0.b3",
      "flags": [],
      "rewrite": "",
      "explanation": ""
    },
    {
      "id": "e0.b4",
      "flags": [
        "weak_opening_verb",
        "no_metric"
      ],
      "rewrite": "Conduct user interviews across [N] sessions",
      "explanation": "Starts with the weak verb \"Ran\" and gives no number of interviews conducted."
    },
    {
      "id": "e1.b0",
      "flags": [
        "no_metric",
        "vague_claim"
      ],
      "rewrite": "Design [N] websites and apps for [N] clients",
      "explanation": "Uses the vague phrase \"various clients\" and lacks a count of websites and apps designed."
    },
    {
      "id": "e1.b1",
      "flags": [
        "passive_voice",
        "no_metric"
      ],
      "rewrite": "Managed the agency's usability testing process across [N] studies",
      "explanation": "Uses the passive phrase \"Was in charge of\" and lacks metrics on testing volume."
    },
    {
      "id": "e1.b2",
      "flags": [],
      "rewrite": "",
      "explanation": ""
    },
    {
      "id": "e1.b3",
      "flags": [
        "no_metric",
        "vague_claim"
      ],
      "rewrite": "Partner across [N] teams to deliver products",
      "explanation": "Uses the vague phrase \"great products\" and lacks any metric for team size or collaboration scope."
    }
  ],
  "overall": "The resume has strong pockets of quantifiable achievements, but many bullets start with weak verbs or lack metrics where scale is plausible. Refining these will make the impact much sharper.",
  "summary_review": "The professional summary relies on clichés like \"loves solving problems\" and \"always learning\" with no specifics about design expertise or career focus. It should be replaced with a concise statement highlighting years of experience, core design specializations, and industry impact."
}
```

## Text and layout extraction — success
```json
{
  "layout": {
    "fonts": [
      "Helvetica",
      "Helvetica-Bold"
    ],
    "pages": [
      {
        "page": 1,
        "links": 0,
        "images": 2,
        "tables": [
          {
            "rows": 3,
            "columns": 2
          }
        ],
        "columns": 2,
        "text_chars": 1075,
        "icon_images": 2,
        "left_blocks": 12,
        "text_blocks": 26,
        "large_images": 0,
        "right_blocks": 24,
        "footer_zone_text": "",
        "header_zone_text": "marcus.lee@example.com   |   +1 415 555 0147   |   Austin, TX",
        "side_by_side_pairs": 10
      }
    ],
    "font_count": 2,
    "page_count": 1,
    "section_headers": [
      "Marcus Lee",
      "TOOLBOX",
      "HTML / CSS",
      "LANGUAGES",
      "ABOUT ME",
      "MY JOURNEY",
      "WHERE I STUDIED",
      "SKILL LEVELS"
    ],
    "total_text_chars": 1075,
    "nonstandard_headers": [
      "Marcus Lee",
      "TOOLBOX",
      "HTML / CSS",
      "ABOUT ME",
      "MY JOURNEY",
      "WHERE I STUDIED",
      "SKILL LEVELS"
    ],
    "contact_in_header_footer": true
  },
  "needs_ocr": false,
  "char_count": 1074,
  "page_count": 1
}
```

## Rewrite Agent — success
_gemini / gemini-3.5-flash-lite, 5385 ms, 2 reply(ies)_
_warnings: ["the summary contains numbers that aren't in the resume: ['6']"]_
_reply 1 rejected: ["~e1.b0 belongs to a PAST role: open it with a past-tense verb, not 'Design'", "~e1.b3 belongs to a PAST role: open it with a past-tense verb, not 'Partner'", "~the summary contains numbers that aren't in the resume: ['7']"]_
_reply 2 rejected: ["~the summary contains numbers that aren't in the resume: ['6']"]_
```json
{
  "notes": "Reordered bullets within the current role to prioritize job-matching items such as the appointment booking flow redesign and design system creation. Applied the Content Agent's approved rewrites using correct past-tense verbs for past roles and present-tense verbs for current roles while preserving all source IDs and placeholder structures.",
  "skills": [
    "Figma",
    "Prototyping",
    "Design systems",
    "User research",
    "HTML / CSS",
    "Sketch",
    "Adobe XD",
    "English",
    "Spanish"
  ],
  "summary": "Senior Product Designer with over 6 years of experience specializing in mobile product design, design systems, and user research. Proven track record of improving user completion rates through targeted flow redesigns.",
  "experience": [
    {
      "dates": "2020 - Present",
      "title": "Lead Product Designer",
      "bullets": [
        {
          "text": "Redesigned the appointment booking flow, which improved completion by 23%",
          "source_id": "e0.b1"
        },
        {
          "text": "Created a design system used by 3 product teams",
          "source_id": "e0.b3"
        },
        {
          "text": "Design the patient mobile app for [N] users across [N] platforms",
          "source_id": "e0.b0"
        },
        {
          "text": "Collaborate with [N] developers and product managers on [N] features",
          "source_id": "e0.b2"
        },
        {
          "text": "Conduct user interviews across [N] sessions",
          "source_id": "e0.b4"
        }
      ],
      "company": "Lumen Health"
    },
    {
      "dates": "2016 - 2020",
      "title": "UX Designer",
      "bullets": [
        {
          "text": "Delivered 40+ projects for startups and retailers",
          "source_id": "e1.b2"
        },
        {
          "text": "Designed [N] websites and apps for [N] clients",
          "source_id": "e1.b0"
        },
        {
          "text": "Managed the agency's usability testing process across [N] studies",
          "source_id": "e1.b1"
        },
        {
          "text": "Partnered across [N] teams to deliver products",
          "source_id": "e1.b3"
        }
      ],
      "company": "Brightpath Agency"
    }
  ]
}
```

## Job-Match Agent — success
_groq / openai/gpt-oss-20b, 29778 ms, 1 reply(ies)_
```json
{
  "summary": "The resume demonstrates strong experience in mobile product design, design systems, and measurable improvements, aligning well with many of the core responsibilities. However, it lacks explicit evidence of mentoring, accessibility compliance, and full end-to-end patient flow coverage beyond booking.",
  "match_score": 75,
  "matched_keywords": [
    {
      "keyword": "mobile app design",
      "evidence": "e0.b0"
    },
    {
      "keyword": "appointment booking flow redesign",
      "evidence": "e0.b1"
    },
    {
      "keyword": "design system",
      "evidence": "e0.b3"
    },
    {
      "keyword": "user interviews",
      "evidence": "e0.b4"
    },
    {
      "keyword": "usability testing",
      "evidence": "e1.b1"
    },
    {
      "keyword": "Figma",
      "evidence": "Skills list"
    },
    {
      "keyword": "HTML/CSS",
      "evidence": "Skills list"
    },
    {
      "keyword": "prototyping",
      "evidence": "Skills list"
    },
    {
      "keyword": "6+ years experience",
      "evidence": "Total years of experience (7 years)"
    }
  ],
  "missing_keywords": [
    {
      "keyword": "messaging flow",
      "importance": "high",
      "suggestion": "No existing experience covers messaging; this is a true gap."
    },
    {
      "keyword": "prescriptions flow",
      "importance": "high",
      "suggestion": "No existing experience covers prescriptions; this is a true gap."
    },
    {
      "keyword": "shipped UI",
      "importance": "medium",
      "suggestion": "No explicit mention of shipped UI; consider adding details if applicable."
    },
    {
      "keyword": "define success metrics",
      "importance": "medium",
      "suggestion": "No explicit evidence of metric definition; consider adding if you have done so."
    },
    {
      "keyword": "experiments",
      "importance": "medium",
      "suggestion": "No explicit mention of experiments; consider adding if you have run experiments."
    },
    {
      "keyword": "mentor designers",
      "importance": "high",
      "suggestion": "No evidence of mentoring; this is a true gap."
    },
    {
      "keyword": "accessibility (WCAG)",
      "importance": "high",
      "suggestion": "No evidence of WCAG compliance; this is a true gap."
    },
    {
      "keyword": "raise quality bar",
      "importance": "medium",
      "suggestion": "No evidence of raising quality bar; consider adding if applicable."
    }
  ],
  "requirement_matches": [
    {
      "note": "Covers booking flow and research, but lacks messaging/prescriptions and explicit shipped UI.",
      "strength": "partial",
      "bullet_ids": [
        "e0.b0",
        "e0.b1",
        "e0.b4"
      ],
      "requirement": "Own end-to-end design for core patient flows (booking, messaging, prescriptions), from research to shipped UI"
    },
    {
      "note": "Shows research and testing, but no explicit design decisions.",
      "strength": "partial",
      "bullet_ids": [
        "e0.b4",
        "e1.b1"
      ],
      "requirement": "Run user research and usability testing and turn findings into design decisions"
    },
    {
      "note": "Design system built and maintained; partnership with engineers implied; Figma usage inferred from skills.",
      "strength": "partial",
      "bullet_ids": [
        "e0.b3",
        "e0.b2"
      ],
      "requirement": "Build and maintain our design system in Figma and partner with engineers on implementation"
    },
    {
      "note": "Shows measurable improvement but lacks metric definition and experiments.",
      "strength": "weak",
      "bullet_ids": [
        "e0.b1"
      ],
      "requirement": "Define success metrics with product managers and show impact through experiments"
    },
    {
      "note": "No evidence of mentoring or quality improvement.",
      "strength": "none",
      "bullet_ids": [],
      "requirement": "Mentor designers and raise the quality bar"
    },
    {
      "note": "Meets years and mobile design experience.",
      "strength": "strong",
      "bullet_ids": [
        "e0.b0"
      ],
      "requirement": "6+ years in product or UX design with a strong mobile portfolio"
    },
    {
      "note": "Skills listed but no bullet evidence of expertise.",
      "strength": "partial",
      "bullet_ids": [],
      "requirement": "Expert Figma and prototyping skills; comfortable with HTML/CSS"
    },
    {
      "note": "Design system at scale present; accessibility missing.",
      "strength": "partial",
      "bullet_ids": [
        "e0.b3"
      ],
      "requirement": "Experience with accessibility (WCAG) and design systems at scale"
    },
    {
      "note": "Shows 23% improvement in booking flow.",
      "strength": "strong",
      "bullet_ids": [
        "e0.b1"
      ],
      "requirement": "A track record of measurable improvements to conversion or task completion"
    }
  ]
}
```

## Final result

**Stats:** {"jd": true, "ats_score": 35, "corrections": 1, "flag_counts": {"no_metric": 6, "vague_claim": 3, "passive_voice": 1, "weak_opening_verb": 3}, "match_score": 75, "ats_blockers": 2, "ats_warnings": 5, "missing_high": ["messaging flow", "prescriptions flow", "mentor designers", "accessibility (WCAG)"], "placeholders": 9, "ats_remaining": ["Ambiguous date format"], "bullets_total": 9, "keywords_total": 17, "bullets_flagged": 6, "summary_changed": false, "keywords_matched": 9, "bullets_reordered": 7, "bullets_rewritten": 6, "ats_fixed_by_reformat": 6}

**Rewrite notes:** Reordered bullets within the current role to prioritize job-matching items such as the appointment booking flow redesign and design system creation. Applied the Content Agent's approved rewrites using correct past-tense verbs for past roles and present-tense verbs for current roles while preserving all source IDs and placeholder structures.

**Guardrail corrections:**
- summary: the new summary contained numbers that aren't in the resume; kept the original

### Before / after, per bullet

- **e0.b1** (Lumen Health) moved []
  - before: Redesigned the appointment booking flow, which improved completion by 23%
- **e0.b3** (Lumen Health) moved []
  - before: Created a design system used by 3 product teams
- **e0.b0** (Lumen Health) CHANGED ['weak_opening_verb', 'no_metric']
  - before: Responsible for the design of the patient mobile app
  - after:  Design the patient mobile app for [N] users across [N] platforms
- **e0.b2** (Lumen Health) CHANGED ['weak_opening_verb', 'no_metric', 'vague_claim']
  - before: Worked with developers and product managers on many features
  - after:  Collaborate with [N] developers and product managers on [N] features
- **e0.b4** (Lumen Health) CHANGED ['weak_opening_verb', 'no_metric']
  - before: Ran user interviews
  - after:  Conduct user interviews across [N] sessions
- **e1.b2** (Brightpath Agency) moved []
  - before: Delivered 40+ projects for startups and retailers
- **e1.b0** (Brightpath Agency) CHANGED ['no_metric', 'vague_claim']
  - before: Designed websites and apps for various clients
  - after:  Designed [N] websites and apps for [N] clients
- **e1.b1** (Brightpath Agency) CHANGED ['passive_voice', 'no_metric']
  - before: Was in charge of the agency's usability testing process
  - after:  Managed the agency's usability testing process across [N] studies
- **e1.b3** (Brightpath Agency) CHANGED ['no_metric', 'vague_claim']
  - before: Collaborated across teams to make great products
  - after:  Partnered across [N] teams to deliver products

### Rewritten draft

**Marcus Lee** · marcus.lee@example.com · +1 415 555 0147 · Austin, TX

**Summary.** Creative designer who loves solving problems and making things beautiful.
Always learning, always curious. Coffee enthusiast.

**Lead Product Designer, Lumen Health** (2020 - Now)
- Redesigned the appointment booking flow, which improved completion by 23%
- Created a design system used by 3 product teams
- Design the patient mobile app for [N] users across [N] platforms
- Collaborate with [N] developers and product managers on [N] features
- Conduct user interviews across [N] sessions

**UX Designer, Brightpath Agency** (2016 - 2020)
- Delivered 40+ projects for startups and retailers
- Designed [N] websites and apps for [N] clients
- Managed the agency's usability testing process across [N] studies
- Partnered across [N] teams to deliver products

**Education.** BFA Graphic Design, University of Texas at Austin (2012 - 2016)

**Skills.** Figma, Prototyping, Design systems, User research, HTML / CSS, Sketch, Adobe XD, English, Spanish
