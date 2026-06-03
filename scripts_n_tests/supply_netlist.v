/* Differential AND chain with top-level supply ports, for testing supply-rail
 * wiring of inserted buffers. With N=2 the third gate's output exceeds the depth
 * limit, so one BUFD is inserted on the (w3, w3n) pair — and its VDD/VSS pins
 * must be wired to the vdd/vss top ports via supply_netlist.supplies.json. */

module supply_chain(a, an, b, bn, c, cn, d, dn, e, en, y, yn, vdd, vss);
  input a;   wire a;
  input an;  wire an;
  input b;   wire b;
  input bn;  wire bn;
  input c;   wire c;
  input cn;  wire cn;
  input d;   wire d;
  input dn;  wire dn;
  input e;   wire e;
  input en;  wire en;
  input vdd; wire vdd;
  input vss; wire vss;
  output y;  wire y;
  output yn; wire yn;

  wire w1;  wire w1n;
  wire w2;  wire w2n;
  wire w3;  wire w3n;

  AND2D g1 (
    .A0(a), .A0N(an), .A1(b), .A1N(bn), .Z(w1), .ZN(w1n)
  );
  AND2D g2 (
    .A0(w1), .A0N(w1n), .A1(c), .A1N(cn), .Z(w2), .ZN(w2n)
  );
  AND2D g3 (
    .A0(w2), .A0N(w2n), .A1(d), .A1N(dn), .Z(w3), .ZN(w3n)
  );
  AND2D g4 (
    .A0(w3), .A0N(w3n), .A1(e), .A1N(en), .Z(y), .ZN(yn)
  );
endmodule
