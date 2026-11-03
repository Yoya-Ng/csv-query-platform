// AC1/AC2 的 JDBC 量測輔助程式（由 bench/ac1_ac2.py 以 `java -cp <pgjdbc.jar> bench/JdbcBench.java ...` 呼叫）。
// 單檔原始碼啟動（JDK 11+），不需 Maven。僅供 PoC 測速，CSV 解析只支援合成資料（無引號欄位）。
// 用法：JdbcBench <jdbcUrl> <user> <password> <mode> <table> <file> <repeats>
//   mode = copy_in | batch_in | copy_out
// 每次執行輸出一行 "RUN <秒數>"；copy_in/batch_in 每次前先 TRUNCATE（不計時）。
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import org.postgresql.PGConnection;
import org.postgresql.copy.CopyManager;

public class JdbcBench {
    public static void main(String[] a) throws Exception {
        String url = a[0], user = a[1], pw = a[2], mode = a[3], table = a[4], file = a[5];
        int repeats = Integer.parseInt(a[6]);
        try (Connection c = DriverManager.getConnection(url, user, pw)) {
            CopyManager cm = c.unwrap(PGConnection.class).getCopyAPI();
            for (int r = 0; r < repeats; r++) {
                if (!mode.equals("copy_out")) {
                    try (Statement s = c.createStatement()) { s.execute("TRUNCATE " + table); }
                }
                long t0 = System.nanoTime();
                switch (mode) {
                    case "copy_in" -> {
                        try (Reader in = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8), 8 << 20)) {
                            cm.copyIn("COPY " + table + " FROM STDIN (FORMAT csv, HEADER true, NULL '')", in, 8 << 20);
                        }
                    }
                    case "batch_in" -> batchIn(c, table, file);
                    case "copy_out" -> {
                        try (OutputStream out = new BufferedOutputStream(new FileOutputStream(file + ".jdbc.out"), 8 << 20)) {
                            cm.copyOut("COPY (SELECT * FROM " + table + ") TO STDOUT (FORMAT csv, HEADER true, NULL '')", out);
                        }
                    }
                    default -> throw new IllegalArgumentException(mode);
                }
                double sec = (System.nanoTime() - t0) / 1e9;
                System.out.printf("RUN %.4f%n", sec);
            }
        }
    }

    static void batchIn(Connection c, String table, String file) throws Exception {
        c.setAutoCommit(false);
        try (BufferedReader br = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8), 8 << 20)) {
            String[] hdr = br.readLine().split(",", -1);
            StringBuilder sql = new StringBuilder("INSERT INTO " + table + " (" + String.join(",", hdr) + ") VALUES (");
            for (int i = 0; i < hdr.length; i++) sql.append(i == 0 ? "?" : ",?");
            sql.append(")");
            try (PreparedStatement ps = c.prepareStatement(sql.toString())) {
                String line; int n = 0;
                while ((line = br.readLine()) != null) {
                    String[] v = line.split(",", -1);
                    for (int i = 0; i < v.length; i++) {
                        if (v[i].isEmpty()) ps.setNull(i + 1, Types.NULL);
                        else ps.setObject(i + 1, v[i], Types.OTHER);   // 讓伺服器依欄位型別推斷
                    }
                    ps.addBatch();
                    if (++n % 1000 == 0) ps.executeBatch();
                }
                ps.executeBatch();
            }
        }
        c.commit();
    }
}
