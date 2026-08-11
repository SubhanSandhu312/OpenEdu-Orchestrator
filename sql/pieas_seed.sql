-- ============================================================================
--  PIEAS LMS  --  legacy source database (simulated)
--
--  This stands in for the real PIEAS LMS: a plain relational store with no API
--  and no webhooks. The ONLY thing that makes synchronization possible is the
--  `last_updated` column on every table, which MySQL maintains automatically:
--
--      last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
--
--  The Orchestrator watermarks against that column. Nothing else here is
--  sync-aware -- no dirty flags, no triggers, no outbox. Zero modifications to
--  the legacy source, exactly as the design requires.
--
--  Run:  python run.py seed
-- ============================================================================

DROP DATABASE IF EXISTS pieas_lms;
CREATE DATABASE pieas_lms CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE pieas_lms;

-- ---------------------------------------------------------------- departments
CREATE TABLE departments (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    dept_name    VARCHAR(128) NOT NULL,
    dept_code    VARCHAR(16)  NOT NULL UNIQUE,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_dept_updated (last_updated)
) ENGINE=InnoDB;

-- -------------------------------------------------------------------- courses
CREATE TABLE courses (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    course_title  VARCHAR(128) NOT NULL,
    course_code   VARCHAR(16)  NOT NULL UNIQUE,
    department_id INT          NOT NULL,
    credit_hours  DECIMAL(4,1) DEFAULT 3.0,
    last_updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (department_id) REFERENCES departments(id),
    INDEX idx_course_updated (last_updated)
) ENGINE=InnoDB;

-- -------------------------------------------------------------------- batches
CREATE TABLE batches (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    batch_name   VARCHAR(32) NOT NULL,
    batch_code   VARCHAR(16) NOT NULL UNIQUE,
    course_id    INT  NOT NULL,
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (course_id) REFERENCES courses(id),
    INDEX idx_batch_updated (last_updated)
) ENGINE=InnoDB;

-- ------------------------------------------------------------------- subjects
CREATE TABLE subjects (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    subject_title VARCHAR(128) NOT NULL,
    subject_code  VARCHAR(32)  NOT NULL UNIQUE,
    department_id INT NOT NULL,
    credits       DECIMAL(4,1) DEFAULT 3.0,
    subject_kind  ENUM('theory','practical','both') DEFAULT 'theory',
    last_updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (department_id) REFERENCES departments(id),
    INDEX idx_subject_updated (last_updated)
) ENGINE=InnoDB;

-- -------------------------------------------------------------------- faculty
CREATE TABLE faculty (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    first_name    VARCHAR(64) NOT NULL,
    last_name     VARCHAR(64) NOT NULL,
    email         VARCHAR(128) UNIQUE,
    phone         VARCHAR(32),
    gender        ENUM('m','f') DEFAULT 'm',
    date_of_birth DATE,
    department_id INT,
    designation   VARCHAR(64),
    last_updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (department_id) REFERENCES departments(id),
    INDEX idx_faculty_updated (last_updated)
) ENGINE=InnoDB;

-- ------------------------------------------------------------------- students
CREATE TABLE students (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    reg_no        VARCHAR(20) NOT NULL UNIQUE,
    first_name    VARCHAR(64) NOT NULL,
    last_name     VARCHAR(64) NOT NULL,
    email         VARCHAR(128) UNIQUE,
    phone         VARCHAR(32),
    gender        ENUM('m','f') DEFAULT 'm',
    date_of_birth DATE,
    course_id     INT,
    batch_id      INT,
    last_updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (course_id) REFERENCES courses(id),
    FOREIGN KEY (batch_id)  REFERENCES batches(id),
    INDEX idx_student_updated (last_updated)
) ENGINE=InnoDB;

-- ----------------------------------------------------- exams (-> op.exam)
CREATE TABLE exams (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    exam_title   VARCHAR(128) NOT NULL,
    exam_code    VARCHAR(16)  NOT NULL UNIQUE,
    subject_id   INT NOT NULL,
    course_id    INT NOT NULL,
    batch_id     INT,
    exam_date    DATETIME NOT NULL,
    end_datetime DATETIME NOT NULL,
    total_marks  INT DEFAULT 100,
    passing_marks INT DEFAULT 40,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (subject_id) REFERENCES subjects(id),
    FOREIGN KEY (course_id)  REFERENCES courses(id),
    FOREIGN KEY (batch_id)   REFERENCES batches(id),
    INDEX idx_exam_updated (last_updated)
) ENGINE=InnoDB;

-- ------------------------------------- exam_results (-> op.exam.attendees)
CREATE TABLE exam_results (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    exam_id       INT NOT NULL,
    student_id    INT NOT NULL,
    marks_obtained INT DEFAULT 0,
    attendance    ENUM('present','absent') DEFAULT 'present',
    remarks       VARCHAR(255),
    last_updated  TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (exam_id)    REFERENCES exams(id),
    FOREIGN KEY (student_id) REFERENCES students(id),
    UNIQUE KEY uq_exam_student (exam_id, student_id),
    INDEX idx_result_updated (last_updated)
) ENGINE=InnoDB;

-- ============================================================================
--  Dummy data
-- ============================================================================

INSERT INTO departments (dept_name, dept_code) VALUES
    ('Department of Physics and Applied Mathematics', 'DPAM'),
    ('Department of Electrical Engineering',          'DEE'),
    ('Department of Mechanical Engineering',          'DME'),
    ('Department of Computer and Information Sciences','DCIS'),
    ('Department of Chemical Engineering',            'DCHE'),
    ('Department of Nuclear Engineering',             'DNE');

INSERT INTO courses (course_title, course_code, department_id, credit_hours) VALUES
    ('BS Physics',                        'BSPHY', 1, 136.0),
    ('MS Applied Mathematics',            'MSAM',  1,  30.0),
    ('BS Electrical Engineering',         'BSEE',  2, 136.0),
    ('BS Mechanical Engineering',         'BSME',  3, 136.0),
    ('BS Computer Science',               'BSCS',  4, 133.0),
    ('MS Computer Science',               'MSCS',  4,  30.0),
    ('BS Chemical Engineering',           'BSCHE', 5, 136.0),
    ('MS Nuclear Engineering',            'MSNE',  6,  30.0);

INSERT INTO batches (batch_name, batch_code, course_id, start_date, end_date) VALUES
    ('BSPHY 2022-2026', 'BPHY22', 1, '2022-09-01', '2026-06-30'),
    ('MSAM 2024-2026',  'MAM24',  2, '2024-09-01', '2026-06-30'),
    ('BSEE 2022-2026',  'BEE22',  3, '2022-09-01', '2026-06-30'),
    ('BSEE 2023-2027',  'BEE23',  3, '2023-09-01', '2027-06-30'),
    ('BSME 2023-2027',  'BME23',  4, '2023-09-01', '2027-06-30'),
    ('BSCS 2023-2027',  'BCS23',  5, '2023-09-01', '2027-06-30'),
    ('BSCS 2024-2028',  'BCS24',  5, '2024-09-01', '2028-06-30'),
    ('MSCS 2024-2026',  'MCS24',  6, '2024-09-01', '2026-06-30'),
    ('BSCHE 2023-2027', 'BCHE23', 7, '2023-09-01', '2027-06-30'),
    ('MSNE 2024-2026',  'MNE24',  8, '2024-09-01', '2026-06-30');

INSERT INTO subjects (subject_title, subject_code, department_id, credits, subject_kind) VALUES
    ('Classical Mechanics',          'PHY-301',  1, 3.0, 'theory'),
    ('Quantum Mechanics I',          'PHY-401',  1, 3.0, 'theory'),
    ('Linear Algebra',               'MTH-201',  1, 3.0, 'theory'),
    ('Circuit Analysis',             'EE-201',   2, 4.0, 'both'),
    ('Digital Logic Design',         'EE-221',   2, 4.0, 'both'),
    ('Signals and Systems',          'EE-301',   2, 3.0, 'theory'),
    ('Thermodynamics',               'ME-211',   3, 3.0, 'theory'),
    ('Fluid Mechanics',              'ME-231',   3, 4.0, 'both'),
    ('Data Structures and Algorithms','CS-201',  4, 4.0, 'both'),
    ('Operating Systems',            'CS-301',   4, 3.0, 'both'),
    ('Database Systems',             'CS-311',   4, 3.0, 'both'),
    ('Machine Learning',             'CS-451',   4, 3.0, 'theory'),
    ('Chemical Reaction Engineering','CHE-321',  5, 3.0, 'theory'),
    ('Nuclear Reactor Theory',       'NE-501',   6, 3.0, 'theory');

INSERT INTO faculty (first_name, last_name, email, phone, gender, date_of_birth, department_id, designation) VALUES
    ('Ahmed',   'Raza',    'ahmed.raza@pieas.edu.pk',    '+92-51-9248601', 'm', '1975-04-12', 1, 'Professor'),
    ('Sana',    'Iqbal',   'sana.iqbal@pieas.edu.pk',    '+92-51-9248602', 'f', '1982-11-03', 1, 'Associate Professor'),
    ('Bilal',   'Hussain', 'bilal.hussain@pieas.edu.pk', '+92-51-9248603', 'm', '1979-07-21', 2, 'Professor'),
    ('Nadia',   'Khan',    'nadia.khan@pieas.edu.pk',    '+92-51-9248604', 'f', '1985-02-17', 2, 'Assistant Professor'),
    ('Usman',   'Tariq',   'usman.tariq@pieas.edu.pk',   '+92-51-9248605', 'm', '1980-09-09', 3, 'Associate Professor'),
    ('Hina',    'Aslam',   'hina.aslam@pieas.edu.pk',    '+92-51-9248606', 'f', '1988-06-25', 4, 'Assistant Professor'),
    ('Kamran',  'Sheikh',  'kamran.sheikh@pieas.edu.pk', '+92-51-9248607', 'm', '1976-12-30', 4, 'Professor'),
    ('Ayesha',  'Malik',   'ayesha.malik@pieas.edu.pk',  '+92-51-9248608', 'f', '1990-03-14', 4, 'Lecturer'),
    ('Faisal',  'Mehmood', 'faisal.mehmood@pieas.edu.pk','+92-51-9248609', 'm', '1983-08-08', 5, 'Associate Professor'),
    ('Zainab',  'Shah',    'zainab.shah@pieas.edu.pk',   '+92-51-9248610', 'f', '1986-01-19', 6, 'Assistant Professor');

INSERT INTO students (reg_no, first_name, last_name, email, phone, gender, date_of_birth, course_id, batch_id) VALUES
    ('PIEAS-22-PHY-001','Hamza',  'Yousaf',  'hamza.yousaf@student.pieas.edu.pk',  '+92-300-1000001','m','2004-03-11', 1, 1),
    ('PIEAS-22-PHY-002','Maryam', 'Nawaz',   'maryam.nawaz@student.pieas.edu.pk',  '+92-300-1000002','f','2004-07-22', 1, 1),
    ('PIEAS-24-AM-001', 'Talha',  'Siddiqui','talha.siddiqui@student.pieas.edu.pk','+92-300-1000003','m','2000-01-30', 2, 2),
    ('PIEAS-22-EE-001', 'Zeeshan','Ali',     'zeeshan.ali@student.pieas.edu.pk',   '+92-300-1000004','m','2004-05-15', 3, 3),
    ('PIEAS-22-EE-002', 'Fatima', 'Zahra',   'fatima.zahra@student.pieas.edu.pk',  '+92-300-1000005','f','2004-09-02', 3, 3),
    ('PIEAS-23-EE-001', 'Arslan', 'Haider',  'arslan.haider@student.pieas.edu.pk', '+92-300-1000006','m','2005-02-18', 3, 4),
    ('PIEAS-23-ME-001', 'Danish', 'Iqbal',   'danish.iqbal@student.pieas.edu.pk',  '+92-300-1000007','m','2005-06-27', 4, 5),
    ('PIEAS-23-ME-002', 'Iqra',   'Batool',  'iqra.batool@student.pieas.edu.pk',   '+92-300-1000008','f','2005-04-04', 4, 5),
    ('PIEAS-23-CS-001', 'Bilal',  'Ahmad',   'bilal.ahmad@student.pieas.edu.pk',   '+92-300-1000009','m','2005-08-12', 5, 6),
    ('PIEAS-23-CS-002', 'Sara',   'Javed',   'sara.javed@student.pieas.edu.pk',    '+92-300-1000010','f','2005-11-23', 5, 6),
    ('PIEAS-23-CS-003', 'Umair',  'Farooq',  'umair.farooq@student.pieas.edu.pk',  '+92-300-1000011','m','2005-01-09', 5, 6),
    ('PIEAS-24-CS-001', 'Noor',   'Fatima',  'noor.fatima@student.pieas.edu.pk',   '+92-300-1000012','f','2006-03-19', 5, 7),
    ('PIEAS-24-CS-002', 'Hassan', 'Raza',    'hassan.raza@student.pieas.edu.pk',   '+92-300-1000013','m','2006-05-28', 5, 7),
    ('PIEAS-24-MCS-001','Areeba', 'Shafiq',  'areeba.shafiq@student.pieas.edu.pk', '+92-300-1000014','f','2001-10-06', 6, 8),
    ('PIEAS-24-MCS-002','Owais',  'Rehman',  'owais.rehman@student.pieas.edu.pk',  '+92-300-1000015','m','2001-12-14', 6, 8),
    ('PIEAS-23-CHE-001','Rabia',  'Sultan',  'rabia.sultan@student.pieas.edu.pk',  '+92-300-1000016','f','2005-07-07', 7, 9),
    ('PIEAS-23-CHE-002','Shahzad','Anwar',   'shahzad.anwar@student.pieas.edu.pk', '+92-300-1000017','m','2005-09-16', 7, 9),
    ('PIEAS-24-NE-001', 'Junaid', 'Akhtar',  'junaid.akhtar@student.pieas.edu.pk', '+92-300-1000018','m','2001-02-25', 8,10),
    ('PIEAS-24-NE-002', 'Komal',  'Riaz',    'komal.riaz@student.pieas.edu.pk',    '+92-300-1000019','f','2001-04-13', 8,10),
    ('PIEAS-22-PHY-003','Saad',   'Bin Zaid','saad.binzaid@student.pieas.edu.pk',  '+92-300-1000020','m','2004-12-01', 1, 1);

INSERT INTO exams (exam_title, exam_code, subject_id, course_id, batch_id, exam_date, end_datetime, total_marks, passing_marks) VALUES
    ('Classical Mechanics - Midterm',    'EX-PHY301M', 1, 1, 1, '2026-03-10 09:00:00','2026-03-10 12:00:00',100,40),
    ('Quantum Mechanics I - Final',      'EX-PHY401F', 2, 1, 1, '2026-06-05 09:00:00','2026-06-05 12:00:00',100,40),
    ('Circuit Analysis - Midterm',       'EX-EE201M',  4, 3, 3, '2026-03-12 09:00:00','2026-03-12 11:30:00',100,40),
    ('Digital Logic Design - Final',     'EX-EE221F',  5, 3, 4, '2026-06-08 09:00:00','2026-06-08 12:00:00',100,40),
    ('Thermodynamics - Midterm',         'EX-ME211M',  7, 4, 5, '2026-03-14 09:00:00','2026-03-14 11:30:00',100,40),
    ('Data Structures - Midterm',        'EX-CS201M',  9, 5, 6, '2026-03-16 09:00:00','2026-03-16 11:30:00',100,40),
    ('Operating Systems - Final',        'EX-CS301F', 10, 5, 6, '2026-06-10 09:00:00','2026-06-10 12:00:00',100,40),
    ('Database Systems - Midterm',       'EX-CS311M', 11, 5, 7, '2026-03-18 09:00:00','2026-03-18 11:30:00',100,40),
    ('Machine Learning - Final',         'EX-CS451F', 12, 6, 8, '2026-06-12 09:00:00','2026-06-12 12:00:00',100,40),
    ('Nuclear Reactor Theory - Final',   'EX-NE501F', 14, 8,10, '2026-06-15 09:00:00','2026-06-15 12:00:00',100,40);

INSERT INTO exam_results (exam_id, student_id, marks_obtained, attendance, remarks) VALUES
    (1,  1, 78, 'present', 'Good grasp of Lagrangian mechanics'),
    (1,  2, 85, 'present', 'Excellent'),
    (1, 20, 61, 'present', NULL),
    (2,  1, 72, 'present', NULL),
    (2,  2, 90, 'present', 'Top of class'),
    (3,  4, 66, 'present', NULL),
    (3,  5, 81, 'present', 'Strong analysis'),
    (4,  6, 74, 'present', NULL),
    (5,  7, 58, 'present', 'Needs improvement'),
    (5,  8, 88, 'present', 'Excellent'),
    (6,  9, 92, 'present', 'Outstanding'),
    (6, 10, 79, 'present', NULL),
    (6, 11,  0, 'absent',  'Absent - medical leave'),
    (7,  9, 86, 'present', NULL),
    (7, 10, 71, 'present', NULL),
    (8, 12, 83, 'present', NULL),
    (8, 13, 64, 'present', NULL),
    (9, 14, 89, 'present', 'Distinction'),
    (9, 15, 77, 'present', NULL),
    (10,18, 80, 'present', NULL),
    (10,19, 68, 'present', NULL);
